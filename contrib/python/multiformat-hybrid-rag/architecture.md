# Architecture

This document describes how the RAG system works end-to-end: how documents get into the knowledge base (ingestion) and how users query it (serving).

---

## High-level overview

```
                          INGESTION
                          ========

GCS bucket ──► BQ Object Table (live metadata mirror)
                       │
                       ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ Vertex AI Pipeline (KFP DAG, 3 sequential steps)             │
   │                                                               │
   │ ① preprocess ──► ② chunk & index ──► ③ cleanup (conditional) │
   └───────┬─────────────────────┬─────────────────────────────────┘
           │ HTTP fanout         │ HTTP fanout
           ▼                     ▼
   Cloud Run:                Cloud Run:
   preprocess-service        chunk-index-service
           │                     │
           ▼                     ▼
   BQ.preprocessed          BQ.chunks + VS2 collections


                          SERVING
                          =======

                    ┌───────────────────────────┐
                    │  Cloud Run: app service   │
                    │  FastAPI (port 8000)      │
                    ├───────────────────────────┤
                    │  ┌─────────────────────┐  │
  User ──► agent   ─┤  │  ADK Agent          │  │
           or REST  │  │  (Gemini 3)         │  │
                    │  └──────────┬──────────┘  │
                    │             │ MCP         │
                    │  ┌──────────▼──────────┐  │
                    │  │  MCP Server         │  │
                    │  │  (port 8081)        │  │
                    │  │  ask_knowledge_base │  │
                    │  └──────────┬──────────┘  │
                    └─────────────┼─────────────┘
                                  │
                    ┌─────────────▼─────────────┐
                    │   Vector Search 2.0       │
                    │   chunks + documents      │
                    │   (hybrid search + RRF)   │
                    └───────────────────────────┘
```

---

## Part 1: Ingestion

The ingestion pipeline runs on Vertex AI Pipelines as a KFP DAG with three sequential steps. Each step has automatic retries (up to 2) for transient failures.

### Identity scheme

Every file and chunk has a deterministic, stable ID:

- **file_id** = `MD5(gcs_uri)` — tied to the file's location, not its content. The same formula works in Python (`hashlib.md5`) and BigQuery (`TO_HEX(MD5(uri))`), so both sides can compute and join on it without syncing.
- **chunk_id** = `{file_id}__{chunk_index}` — deterministic given the same file and chunk parameters.

---

### Step 1: Preprocess

**Goal:** detect new or changed files in GCS, extract their text, classify whether the content is relevant, and write the results into the BigQuery `preprocessed` table.

#### 1.1 Change detection

The BQ Object Table is an external table that mirrors live GCS metadata (URIs, md5 hashes, sizes). The pipeline compares it against the `preprocessed` table to find work:

| Condition | What it means |
|---|---|
| File in GCS but not in `preprocessed` | New file — needs extraction |
| File in both but md5 hash differs | Changed file — needs re-extraction |
| File in `preprocessed` but not in GCS | Deleted file — handled by Step 3 |

The comparison is a single SQL query with five named CTEs. It only reads lightweight columns (file_id, content_hash) from `preprocessed` — the heavy `content` column is never scanned during detection.

#### 1.2 Extension allowlist

The SQL query filters files by extension before anything enters the pipeline:

```
.html, .htm, .pdf, .doc, .docx, .ppt, .pptx, .xls, .xlsx, .rtf, .md, .txt, .json, .jsonl
```

Unsupported types (images, videos, archives, `.DS_Store`, etc.) are silently excluded at the database level — no Cloud Run call, no Gemini call, no stub row. Adding a random file type to the bucket is harmless.

#### 1.3 Content deduplication

Before sending files for extraction, the query checks for duplicate content using the md5 hash:

- **Cross-run dedup:** if the same md5 hash was already successfully extracted in a previous run under a different file_id, we skip extraction and write a stub row that references the original.
- **Within-run dedup:** if multiple files in the same batch share an md5 hash, only the one with the lowest URI gets extracted. The rest reference it.

Stubs have `content=''`, `error='duplicate_of:<original_file_id>'`. They satisfy the change-detection query on future runs (the file_id exists in `preprocessed` with a matching content_hash), so duplicates are never re-processed.

#### 1.4 Text extraction (Cloud Run)

The orchestrator dispatches one HTTP request per file to the **preprocess Cloud Run service** via a `ThreadPoolExecutor` with 200 workers. Cloud Run autoscales instances to handle the burst.

The service flow per file:

1. **Download** the file from GCS to a temp directory
2. **Quick text extraction** — a cheap, non-LLM pass (pymupdf for PDFs, BeautifulSoup for HTML, python-docx/python-pptx for Office formats). If this yields 200+ characters, we can classify early.
3. **Early relevance check** — if the quick text is rich enough, run the relevance classifier. If the file is irrelevant, return immediately without the expensive Gemini conversion. This saves one Gemini call per irrelevant file.
4. **Full extraction** — for files that passed the early check (or had sparse raw text), do the full parse. This involves Gemini multimodal calls for PDFs and HTML, LibreOffice conversion for legacy Office formats, and direct rendering for JSON/JSONL.

Supported formats and how they're extracted:

| Format | Quick text | Full extraction |
|---|---|---|
| PDF | pymupdf `get_text()` | pymupdf page split + Gemini multimodal per page |
| HTML | BeautifulSoup `.get_text()` | BS cleanup + Gemini Flash markdown conversion |
| DOCX | python-docx paragraphs + tables | LibreOffice → PDF → Gemini multimodal |
| PPTX | python-pptx slide text | LibreOffice → PDF + python-pptx → Gemini multimodal |
| DOC, PPT, RTF | none (legacy binary) | LibreOffice → PDF → Gemini multimodal |
| XLS | none | xlrd → markdown tables (no LLM) |
| XLSX | none | openpyxl → markdown tables (no LLM) |
| JSON / JSONL | none | Pure Python recursive renderer → markdown (no LLM) |
| TXT, Markdown | n/a | Direct download, no conversion |

#### 1.5 Relevance classification

An LLM classifier decides whether extracted content belongs in the knowledge base. It filters out empty pages, login screens, cookie banners, navigation-only HTML, and non-informational content.

Edge cases:
- Content shorter than 50 characters is automatically irrelevant.
- If the classifier fails (Gemini error), the file defaults to **relevant** — we'd rather index junk than lose real content.

#### 1.6 Staging and merge

Results stream into a per-run staging table in batches of 100 rows. Once all extraction is done, a single `MERGE` upserts from staging into the `preprocessed` table by file_id:

- Existing file_id → UPDATE content, hash, timestamps
- New file_id → INSERT

The staging table is named `_staging_preprocessed_{run_id}` (where run_id includes a timestamp and random suffix), so concurrent pipeline runs can't collide. The staging table is dropped after the merge.

#### 1.7 Edge cases handled

- **Token refresh on long runs:** ID tokens for Cloud Run authentication expire after 60 minutes. The orchestrator mints a fresh token per request using a cache that auto-refreshes 10 minutes before expiry. A 3-hour run with 10,000 files won't silently 401 halfway through.
- **429 rate limits:** Cloud Run's platform returns 429 during scale-up bursts. The HTTP retry logic backs off with jitter and does NOT count 429s toward the retry limit, so transient rate limits don't exhaust retries.
- **HTTP failures become rows:** if extraction fails after all retries, the orchestrator writes a row with `error='http:Timeout: ...'` and `relevant=False`. The next run sees a mismatched content_hash and retries automatically.
- **Jitter on retries:** backoff delay is randomized between 0.5x and 1.5x to prevent thundering herd (200 threads sleeping the same duration then all retrying at once).

---

### Step 2: Chunk & Index

**Goal:** split the extracted text into chunks, enrich each chunk with contextual information via Gemini, push chunks + full documents to Vector Search 2.0, and record the chunks in BigQuery.

#### 2.1 Rechunk detection

The orchestrator identifies which files need chunking using a two-phase query:

1. **Metadata scan** (no content column touched): find file_ids where `extracted_at > MAX(indexed_at)` or that have no chunks at all.
2. **Content fetch**: JOIN only the candidate file_ids back to `preprocessed` to get their text.

This matters because the `content` column is large (full document text). Scanning it for every file would be expensive when only a few files actually need chunking.

The `indexed_at` column (not `chunked_at`) drives re-chunk detection. The orchestrator only stamps `indexed_at` after the Cloud Run service confirms VS2 success, so files that failed halfway are automatically retried on the next run.

A `rechunk_all=True` mode skips the incremental detection and re-chunks every relevant file (useful after changing chunk size or overlap parameters).

#### 2.2 Per-file chunking (Cloud Run)

The orchestrator dispatches one HTTP request per file to the **chunk-index Cloud Run service** via a `ThreadPoolExecutor` with 200 workers. The service handles each file atomically:

1. **Split** — uses LangChain's `MarkdownTextSplitter` (chunk_size=1500, overlap=300 by default). The splitter respects markdown structure: it prefers to break at headers, then paragraphs, then lines.

2. **Enrich** — for each chunk, Gemini generates up to 4 sentences of context explaining what the chunk is about relative to the full document. This runs in parallel (16 threads per request).

   Context window for enrichment:
   - If the document is under 500K characters: the full document is sent as context for every chunk.
   - If larger: a sliding window of 50K characters before and after the chunk's position. This way chunks at the end of a long document get local context instead of always being described in terms of the document's opening.

   If enrichment fails after 3 retries, the chunk ships without context — it's still searchable on its own text, just without the situational description. Failed enrichments are counted and logged so you can monitor enrichment health.

3. **Delete old VS2 chunks** — query the chunks collection for any existing data objects with this file_id, and batch-delete them. This happens only after the service has the new chunks ready, so a service outage means users see stale results (not empty results).

4. **Push new chunks to VS2** — create data objects in the chunks collection with empty vectors. VS2 auto-generates embeddings using the configured model (gemini-embedding-001, 3072 dims) and the collection's text_template. The context is prepended to the chunk text before embedding so the embedding captures the situational meaning.

5. **Upsert full document to VS2** — atomically update (or create) the full document text in the documents collection. Uses `update_data_object` first; if the file_id doesn't exist yet, falls back to `batch_create_data_objects`. This avoids a crash window where the document is deleted but not yet re-created.

6. **Return chunks** — the service returns the chunk data so the orchestrator can insert it into BQ.

#### 2.3 BQ chunk staging

Same pattern as preprocess: chunks stream into a per-run staging table in batches, then a single `INSERT` moves them to the target table with `chunked_at` and `indexed_at` stamped server-side.

Old BQ chunks for successfully re-indexed files are deleted in batches of 100 (not one-by-one per file) to keep the number of DELETE operations manageable.

#### 2.4 Edge cases handled

- **Delete-on-success:** old chunks are only deleted from BQ and VS2 after the service confirms the new chunks are ready. During a service outage, search keeps working with the previous chunks.
- **Atomic document upsert:** the documents collection uses `update_data_object` (atomic in-place replace) instead of delete-then-create. If the process crashes, the old document is still there.
- **Per-run staging tables:** same run_id isolation as preprocess, preventing concurrent runs from interfering.
- **Sliding window context:** prevents the tail of a 200-page document from getting context built only from page 1.

---

### Step 3: Cleanup (conditional)

**Goal:** detect files deleted from GCS and cascade-remove them from all downstream stores.

Since GCS doesn't emit delete events that the pipeline subscribes to, detection is a batch comparison: `preprocessed LEFT JOIN Object Table WHERE obj.uri IS NULL`. Any file in our `preprocessed` table but missing from the live GCS snapshot was deleted from the bucket.

#### 3.1 Cascade delete order

1. VS2 chunks collection (semantic search index)
2. VS2 documents collection (full-text KV store)
3. BQ chunks table
4. BQ preprocessed table (deleted **last**)

The `preprocessed` row is deleted last on purpose. The detection query checks `preprocessed` first — if the process crashes after deleting VS2 data but before cleaning BQ, the next run re-finds the file in `preprocessed` and retries the full cascade.

#### 3.2 Safety guard

Before cascading, the cleanup step checks that every file marked for deletion belongs to the currently configured GCS bucket. If someone accidentally changes `GCS_BUCKET` in config, the Object Table points at a different bucket and the join would falsely flag the entire old bucket for deletion. The safety guard catches this and aborts with a clear error message.

#### 3.3 Conditional execution

The cleanup step is wrapped in a `dsl.Condition(skip_cleanup == False)` in the KFP DAG, so it can be disabled via a pipeline parameter when you don't want deletion propagation (e.g., during testing or bulk re-ingestion).

---

### Data stores (ingestion side)

#### BigQuery tables

**preprocessed** — one row per file:
```
file_id         STRING    -- MD5(gcs_uri), primary key
gcs_uri         STRING    -- full GCS path
content_hash    STRING    -- GCS md5 hash (for change detection)
content         STRING    -- full extracted text
content_length  INT64     -- LENGTH(content), avoids scanning content column for dedup
file_name       STRING    -- extracted from URI
file_type       STRING    -- file extension
relevant        BOOL      -- LLM classifier result
extracted_at    TIMESTAMP -- set by MERGE on upsert
error           STRING    -- NULL on success, otherwise error detail

CLUSTER BY file_id
```

**chunks** — one row per chunk:
```
chunk_id     STRING     -- "{file_id}__{chunk_index}"
file_id      STRING     -- FK to preprocessed
gcs_uri      STRING     -- source file path
chunk_index  INT64      -- position in the document (0-based)
chunk_text   STRING     -- the chunk text
context      STRING     -- LLM-generated context (may be empty)
chunked_at   TIMESTAMP  -- set on INSERT
indexed_at   TIMESTAMP  -- set only after VS2 confirms success

CLUSTER BY file_id
```

Both tables use `CLUSTER BY file_id` — every pipeline query (change detection, MERGE, DELETE, rechunk detection) joins or filters by file_id, so clustering ensures BigQuery scans only the relevant data blocks.

#### Vector Search 2.0 collections

**Chunks collection** — searchable index:
- Fields: `file_id`, `chunk_id`, `chunk_text`, `gcs_uri`
- Auto-embedding: `chunk_text` is embedded with gemini-embedding-001 (3072 dims, task_type=RETRIEVAL_DOCUMENT)
- Used for hybrid search at query time

**Documents collection** — full-text KV store:
- Fields: `file_id`, `gcs_uri`, `content`
- `data_object_id == file_id` so retrieval is a direct point-lookup (sub-100ms)
- The vector schema is required by VS2 but effectively unused (embeds only the file_id hex string)
- Purpose: return the full document text after search finds matching chunks

---

### Cloud Run services

| Service | CPU | Memory | Concurrency | Min/Max instances |
|---|---|---|---|---|
| preprocess-service | 4 vCPU | 8 GiB | 2 per instance | 5 / 100 |
| chunk-index-service | 2 vCPU | 2 GiB | 1 per instance | 5 / 200 |

The preprocess service uses concurrency=2 (two files processed simultaneously on the same instance) because it's I/O-bound (waiting on Gemini). The chunk-index service uses concurrency=1 because enrichment uses 16 parallel Gemini threads per file and needs the full CPU.

---

### Retry strategy (ingestion)

| Where | Mechanism | Retries | Notes |
|---|---|---|---|
| Orchestrator → Cloud Run | HTTP retry with backoff + jitter | 4 attempts; 429 doesn't count | Cap 30s between retries |
| KFP step crash | KFP `set_retry` | 2 retries per step | Re-runs from scratch (safe because of change detection) |
| Gemini calls (preprocess) | `@retry_with_exponential_backoff` | 5 retries; 429 doesn't count | Never retries 401/403/404 |
| Gemini enrichment (chunk-index) | `@retry_with_exponential_backoff` | 3 retries | Falls back to empty context on failure |
| VS2 calls (chunk-index) | `_retry_call` with exponential backoff | 6 attempts, cap 60s | Covers transient gRPC errors |
| Relevance classifier | try/except | Defaults to relevant=True | Doesn't lose data on classifier outage |

---

## Part 2: Serving

The serving side is a single Cloud Run service that runs everything in one container on one port: FastAPI web server, MCP server (mounted at `/mcp`), and the ADK agent.

### The agent

The agent is built with Google's Agent Development Kit (ADK). It uses Gemini 3 Flash and has a single tool: `ask_knowledge_base`, exposed through MCP.

The agent's instruction tells it to:
- Always search the knowledge base before answering (never rely on its own training data for domain-specific info)
- Base answers strictly on retrieved documents
- Cite source document names when available
- Answer in the same language the user is using (Italian or English)
- Ask for clarification rather than guessing

### The search tool

The `ask_knowledge_base` tool performs a two-stage hybrid search:

**Stage 1 — Hybrid search on chunks:**
- Runs two searches in parallel against the chunks collection:
  - **Semantic search:** the query is embedded and compared against chunk embeddings
  - **Text search (BM25-style):** keyword matching on the `chunk_text` field
- Results are combined using Reciprocal Rank Fusion (RRF) with configurable weights (default: 70% semantic, 30% text)
- Returns up to `top_k` results (default 10)

**Stage 2 — Full document resolution:**
- Walk the ranked chunks, deduplicate by file_id (so the same document isn't returned twice even if multiple chunks match)
- Batch-fetch the full document text from the documents collection via parallel `GetDataObject` calls (up to 32 concurrent)
- If a document is missing from the documents collection (e.g., mid-pipeline drift), fall back to the chunk text from the search result

**Output format:**
```xml
## Context provided:
<Document 0 source="product_catalog.pdf">
Full document text here...
</Document 0>
<Document 1 source="pricing_guide.docx">
Full document text here...
</Document 1>
```

### Three ways to call the search

All three interfaces are available on the same Cloud Run URL. Get it with:

```bash
export SERVICE_URL=$(gcloud run services describe multiformat-hybrid-rag \
  --region=us-central1 --format='value(status.url)')
```

The service requires authentication. For `curl`, use `gcloud auth print-identity-token`.
For local development (`make local-backend`), no auth is needed and the base URL is `http://localhost:8080`.

#### 1. REST endpoint

Call the search directly, useful for integrations that don't need the conversational agent:

```bash
curl -X POST ${SERVICE_URL}/api/search \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{"query": "your search query here", "top_k": 5}'
```

Response:
```json
{
  "result": "## Context provided:\n<Document 0 source=\"pricing.pdf\">\n...\n</Document 0>"
}
```

Parameters:
- `query` (string, required) — the search text
- `top_k` (integer, default 10) — number of unique documents to return

#### 2. MCP server

The MCP server is mounted at `/mcp` on the same port — externally reachable, no separate process needed.

**SSE endpoint:** `${SERVICE_URL}/mcp/sse`

The server exposes one tool:

| Tool | Parameters | Description |
|---|---|---|
| `ask_knowledge_base` | `conversation_summary` (str), `question` (str), `top_k` (int, default 10), `generative_answer` (bool, default true) | Search the knowledge base and return an answer or raw documents |

**`generative_answer=true` (default):** The tool handles the full RAG flow — searches VS2, retrieves a windowed excerpt centered on the matched chunk (10K chars each side), and calls Gemini to generate a grounded answer. The calling agent gets a ready-made response without handling prompting or document formatting.

**`generative_answer=false`:** Returns the raw retrieved documents as-is (full text, no windowing, no LLM). Useful when the calling agent wants to handle RAG prompting itself or needs the raw content.

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "multiformat-hybrid-rag": {
      "url": "https://<SERVICE_URL>/mcp/sse"
    }
  }
}
```

**Other MCP clients** — connect via SSE transport to `https://<SERVICE_URL>/mcp/sse`.

**Local development** — when running `make local-backend`, MCP is at `http://localhost:8080/mcp/sse`.

**Standalone mode** — the MCP server can also run as a separate process (useful for development):

```bash
# SSE transport
python -m app.mcp_server --transport sse --port 8081

# Stdio transport (for agent subprocess communication)
python -m app.mcp_server --transport stdio
```

#### 3. Agent chat (SSE streaming)

Send a message to the ADK agent, which calls `ask_knowledge_base` internally and answers based on the retrieved documents:

```bash
curl -X POST ${SERVICE_URL}/run_sse \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "app",
    "user_id": "test-user",
    "session_id": "test-session",
    "new_message": {
      "role": "user",
      "parts": [{"text": "your question here"}]
    }
  }'
```

Response: Server-Sent Events stream. Each event contains a chunk of the agent's response. The agent internally calls `ask_knowledge_base`, uses the returned documents as context, and generates an answer grounded in the knowledge base.

### How the pieces connect at runtime

When the Cloud Run container starts:

1. FastAPI starts on port 8080 (ADK endpoints + REST search + feedback + MCP)
2. The MCP server is mounted at `/mcp` as a Starlette sub-application (same process, same port)
3. The environment variable `MCP_SERVER_URL` is set to `http://localhost:8080/mcp/sse`
4. When the ADK agent needs to search, it calls `ask_knowledge_base` via MCP (SSE connection to the mounted path)
5. The MCP server calls `search_collection()` which queries VS2

External clients have three entry points on the same URL:
- Agent chat: `POST /run_sse`
- REST search: `POST /api/search`
- MCP: `GET /mcp/sse` (SSE connection for any MCP client)

### Feedback

The `/feedback` endpoint collects user ratings on agent responses:

```
POST /feedback
Content-Type: application/json

{
  "score": 4,
  "text": "Answer was accurate but could be more concise",
  "user_id": "agent-123",
  "session_id": "session-456"
}
```

Feedback is logged as structured JSON to Cloud Logging for analysis.

### Observability

- **Cloud Trace:** distributed tracing across agent calls
- **Cloud Logging:** structured logs from the FastAPI app
- **GCS telemetry:** GenAI completion metadata (no prompt/response content) exported as JSONL to a logs bucket

---

## Configuration

All configuration lives in `.env`. Key groups:

| Variable | Purpose | Default |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | GCP project | — |
| `GOOGLE_CLOUD_LOCATION` | GCP region | us-central1 |
| `GCS_BUCKET` | Source documents bucket | `{GOOGLE_CLOUD_PROJECT}-rag-docs` |
| `GCS_PREFIX` | Folder prefix within bucket | `documents/` |
| `MODEL_NAME` | Agent LLM | gemini-3.7-flash |
| `MARKDOWN_CONVERTER_GEMINI_MODEL` | PDF/HTML extraction | gemini-3.7-flash |
| `CONTEXTUAL_CHUNKING_GEMINI_MODEL` | Chunk enrichment | gemini-3.5-flash-lite |
| `RELEVANCE_GEMINI_MODEL` | Relevance classifier | gemini-3.5-flash-lite |
| `VS_EMBEDDING_MODEL` | Embedding model for VS2 | gemini-embedding-001 |
| `VS_EMBEDDING_DIMS` | Embedding dimensions | 3072 |
| `VS_SEMANTIC_WEIGHT` | Semantic vs text weight in RRF | 0.7 |
| `CHUNK_SIZE` | Max characters per chunk | 1500 |
| `CHUNK_OVERLAP` | Overlap between chunks | 300 |

---

## File map

```
app/
  agent.py              — ADK agent definition (Gemini + MCP tool)
  fast_api_app.py        — FastAPI server (REST search, feedback, ADK endpoints, MCP startup)
  mcp_server.py          — MCP server exposing ask_knowledge_base tool
  vector_search.py       — Hybrid search logic (semantic + text + RRF + document resolution)
  app_utils/
    telemetry.py         — OpenTelemetry + Cloud Logging setup
    typing.py            — Request/Feedback data models

src/
  document_preprocessing/
    preprocess.py        — Change detection, HTTP fanout, staging/merge orchestration
    document_relevance_classifier.py — LLM relevance classifier
    parser/              — Multi-format parsers (PDF, DOCX, HTML, JSON, etc.)
  chunking/
    chunk_and_index.py   — Rechunk detection, HTTP fanout, VS2 bootstrap, staging/insert
  removal/
    propagate_gcs_deletions.py — Deletion detection, safety guard, cascade delete
  utils/
    bq_utils.py          — BQ DDL, staging tables, MERGE/DELETE, change detection queries
    vs_utils.py          — VS2 collection creation, data object CRUD, batch operations
    config.py            — Singleton config from environment
    auth.py              — ID token caching with auto-refresh
    http_utils.py        — HTTP POST with retry, backoff, jitter
    llm_utils.py         — Gemini retry decorator, MIME detection

data_ingestion_pipeline/
  data_ingestion_pipeline/
    pipeline.py          — KFP DAG definition (3-step sequential pipeline)
    submit_pipeline.py   — CLI entry point, Vertex AI submission, scheduling
    components/          — KFP component wrappers (preprocess, chunk_and_index, cleanup)
  preprocess_service/
    main.py              — Cloud Run service: download, parse, classify, return text
  chunk_index_service/
    main.py              — Cloud Run service: split, enrich, VS2 push, return chunks

infra/terraform/dev/
  bigquery.tf            — BQ dataset, Object Table, preprocessed, chunks tables
  preprocess_service.tf  — Cloud Run preprocess service infra
  chunk_index_service.tf — Cloud Run chunk-index service infra
  vector_search.tf       — VS2 collection infra
  service.tf             — App Cloud Run service infra
```
