# RAG Template

A production RAG system on GCP that ingests documents from GCS, extracts and chunks text with contextual enrichment, indexes into Vector Search 2.0, and serves hybrid search through a Gemini-powered agent, a REST API, and an MCP server — all from a single Cloud Run service.

For a detailed walkthrough of every component, edge case, and design decision, see [architecture.md](architecture.md).

<table>
  <thead>
    <tr>
      <th colspan="2">Key Features</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Multi-format ingestion:</strong> PDFs (via Gemini multimodal), Office docs (DOC/DOCX/PPT/PPTX/XLSX via LibreOffice), HTML, JSON/JSONL, and Markdown — all converted to clean markdown.</td>
    </tr>
    <tr>
      <td><strong>Contextual chunking:</strong> Markdown-aware splitting with Gemini-generated per-chunk context prepended before embedding, so vectors capture meaning within the full document.</td>
    </tr>
    <tr>
      <td><strong>Hybrid search:</strong> Semantic embeddings + BM25 keyword matching combined via Reciprocal Rank Fusion on Vector Search 2.0.</td>
    </tr>
    <tr>
      <td><strong>Three serving interfaces:</strong> ADK agent chat, REST search API, and MCP server — all from a single Cloud Run deployment.</td>
    </tr>
  </tbody>
</table>

## Agent Details

| Attribute | Description |
| :--- | :--- |
| **Interaction Type** | Conversational / API |
| **Complexity** | Intermediate |
| **Agent Type** | RAG (Retrieval-Augmented Generation) |
| **Components** | Gemini 3, Vector Search 2.0, BigQuery, Cloud Run, Vertex AI Pipelines |
| **Vertical** | Any (document Q&A) |

## Getting Started

### Prerequisites
- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** (Python package manager)
- **[Google Cloud SDK](https://cloud.google.com/sdk/docs/install)** (`gcloud` CLI)
- **[Terraform](https://developer.hashicorp.com/terraform/install)** (infrastructure provisioning)
- **make** (pre-installed on macOS/Linux)

### Step 1: Get the Code

**Option A: Clone directly from adk-recipes**
```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/contrib/python/multiformat-hybrid-rag
```

**Option B: Create project from template**

This uses the [Google Agents CLI](https://github.com/google/agents-cli) to create a new directory with all the necessary code, plus optional CI/CD and deployment scaffolding.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this recipe**:

```bash
agents-cli create my_rag_app -a adk@multiformat-hybrid-rag
```

You'll be prompted to select a deployment option — choose **None** if you want to use the deployment already included in the repo, or pick one to get Google Agents CLI CI/CD scaffolding.

### Step 2: Configure Environment

The recipe ships `.env.example` as a template; `.env` itself is gitignored, so
create your own copy first:

```bash
cp .env.example .env
```

Then edit `.env` and set at least:
```env
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
```
All other variables have sensible defaults and can be customized later.
`.env.example` is also read at runtime as a fallback, so anything you leave
out falls back to the committed default rather than being empty.

### Step 3: Install Dependencies
```bash
make install
```

### Step 4: Authenticate with Google Cloud
```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project your-gcp-project-id
```

### Step 5: Infrastructure Setup

Enables necessary APIs, provisions BigQuery tables, VS2 collections, Cloud Run pipeline services, and IAM bindings via Terraform:
```bash
make setup-infra
```

### Step 6: Run the Ingestion Pipeline

Upload documents to the GCS bucket (`gs://<GOOGLE_CLOUD_PROJECT>-rag-docs/documents/`), then:
```bash
make data-ingestion-remote
```

This submits a Vertex AI Pipeline that preprocesses, chunks, and indexes your documents. It also sets a daily cron schedule for incremental updates.

### Step 7: Deploy the Serving App
```bash
make deploy
```

### Step 8: Local Development
```bash
# Run the full app locally (agent + REST + MCP)
make local-backend

# Or just the ADK playground UI
make playground
```

## How it works

**Ingestion** — a 3-step Vertex AI Pipeline runs periodically:

1. **Preprocess** — detects new/changed files in GCS, extracts text (PDFs via Gemini multimodal, Office via LibreOffice, JSON/HTML/Markdown natively), classifies relevance, deduplicates by content hash
2. **Chunk & Index** — splits text into markdown-aware chunks, enriches each chunk with Gemini-generated context, pushes to Vector Search 2.0 (semantic + keyword search), records in BigQuery
3. **Cleanup** — detects files deleted from GCS and cascade-removes from VS2 and BigQuery

**Serving** — a single Cloud Run service exposes three interfaces:

- **Agent chat** (`POST /run_sse`) — Gemini 3 agent that searches the knowledge base via MCP before answering
- **REST search** (`POST /api/search`) — direct hybrid search (semantic + BM25 with Reciprocal Rank Fusion)
- **MCP server** (`GET /mcp/sse`) — the `ask_knowledge_base` tool, connectable from any MCP client

## Project structure

```
app/                          # Serving (Cloud Run)
  agent.py                      ADK agent (Gemini + MCP tool)
  fast_api_app.py               FastAPI server (REST + agent + MCP mount)
  mcp_server.py                 MCP server (ask_knowledge_base tool)
  vector_search.py              Hybrid search logic (VS2)

src/                          # Ingestion logic (shared package)
  document_preprocessing/       Step 1: change detection, extraction, relevance
  chunking/                     Step 2: splitting, enrichment, VS2 indexing
  removal/                      Step 3: cascade deletion of removed files
  utils/                        BQ, VS2, auth, HTTP retry, config utilities

data_ingestion_pipeline/               # Cloud Run services + KFP pipeline
  preprocess_service/           Cloud Run: per-file text extraction
  chunk_index_service/          Cloud Run: per-file chunking + VS2 push
  data_ingestion_pipeline/      KFP DAG definition + Vertex AI submission

infra/terraform/dev/     # Infrastructure as code
```

## Commands

| Command | Description |
|---|---|
| `make install` | Install dependencies |
| `make setup-infra` | Build images + provision infrastructure (Terraform) |
| `make tf-plan` | Preview infrastructure changes |
| `make data-ingestion-remote` | Run the ingestion pipeline and set daily schedule |
| `make deploy` | Deploy the serving app to Cloud Run |
| `make local-backend` | Run the app locally with hot-reload |
| `make playground` | Launch ADK playground UI |
| `make mcp-server` | Run standalone MCP server (SSE) |
| `make test` | Run tests |
| `make lint` | Run code quality checks |

## Using the search API

Once deployed, the service exposes three interfaces on the same URL. Get the service URL with:

```bash
gcloud run services describe multiformat-hybrid-rag --region=us-central1 --format='value(status.url)'
```

### REST endpoint

Direct search without the agent layer:

```bash
curl -X POST https://<SERVICE_URL>/api/search \
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

### MCP server

Connect any MCP client (Claude Desktop, other agents, custom tools) to:

```
https://<SERVICE_URL>/mcp/sse
```

The server exposes one tool:

| Tool | Parameters | Returns |
|---|---|---|
| `ask_knowledge_base` | `conversation_summary` (str), `question` (str), `top_k` (int, default 10), `generative_answer` (bool, default true) | A direct answer or raw documents |

Two modes:
- **`generative_answer=true`** (default) — searches, retrieves windowed excerpts centered on matched chunks, calls Gemini to generate a grounded answer. The calling agent gets a ready-made response.
- **`generative_answer=false`** — returns the raw retrieved documents (full text, no LLM). Useful when the caller wants to handle RAG prompting itself.

Example MCP client config (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "multiformat-hybrid-rag": {
      "url": "https://<SERVICE_URL>/mcp/sse"
    }
  }
}
```

For local development, the MCP server is available at `http://localhost:8000/mcp/sse` when running `make local-backend`.

### Agent chat (SSE)

Stream a conversation with the Gemini agent (it calls `ask_knowledge_base` internally):

```bash
curl -X POST https://<SERVICE_URL>/run_sse \
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

Response: Server-Sent Events stream with the agent's answer.

## Pipeline walkthrough

The notebook [pipeline_walkthrough.ipynb](pipeline_walkthrough.ipynb) walks through every ingestion step using local example files from `example_data/`. Run it to see extraction, classification, chunking, and contextual enrichment in action.

## Configuration

All configuration is in `.env`. Key variables:

| Variable | Purpose |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | GCP project (required) |
| `GOOGLE_CLOUD_LOCATION` | GCP region (default: us-central1) |
| `GCS_BUCKET` | Source documents bucket |
| `MODEL_NAME` | Agent LLM (default: gemini-3.7-flash) |
| `MODEL_NAME_MCP_TOOL` | MCP tool answer generation (default: gemini-3.5-flash-lite) |
| `VS_SEMANTIC_WEIGHT` | Semantic vs keyword weight in search (default: 0.7) |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | Chunking parameters (default: 1500 / 300) |

See `.env` for the full list.

## Disclaimer

This agent recipe is provided for illustrative purposes only. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this recipe. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.
