# RAG Agent — Agent Platform Search

A starter RAG agent that answers questions grounded on documents indexed in
**Agent Platform Search** (Discovery Engine). Documents land in a GCS bucket and
are ingested automatically by a GCS Data Connector — no separate ingestion
pipeline is required.

## Agent Details
| Attribute | Detail |
| :-- | :-- |
| Interaction Type | Conversational |
| Complexity | Intermediate |
| Agent Type | Single Agent |
| Components | Tools, RAG, Terraform, Evaluation |

## How it works
- `app/agent.py` — ADK agent wiring the search tool.
- `app/retrievers.py` — `create_search_tool()` returns a `VertexAiSearchTool`
  bound to the data store (or a mock when `INTEGRATION_TEST=TRUE`).
- `infra/terraform/` — provisions a docs GCS bucket, a GCS Data Connector, and a
  search engine over the auto-created data store.

## Setup
1. `cp .env.example .env` and set `GOOGLE_CLOUD_PROJECT`.
2. Provision the datastore: edit `infra/terraform/vars/env.tfvars`, then
   `make setup-infra`.
3. Copy the Terraform outputs into `.env`: set `DATA_STORE_ID` and
   `DATA_STORE_COLLECTION` from `data_store_id` / `data_store_collection`
   (the connector auto-generates these; the defaults may not match).
4. Load documents: `make upload-sample-data` (uploads the bundled
   `sample_data/`, or copy your own files to the bucket), then `make ingest`
   to trigger an immediate sync (otherwise the connector waits for its daily
   refresh).

## Run

```bash
make install && make playground
```

Select the `app` folder, then ask a question such as
*"What is the payload and battery life of the Atlas-7 robot?"*

## Test
`make test` runs the integration test. The retriever is mocked
(`INTEGRATION_TEST=TRUE`), but the agent still makes a live Gemini call, so
Google Cloud credentials (ADC) and Vertex AI access are required. Without
credentials the test is skipped.

## CI/CD

These recipes don't bundle a deployment pipeline. Documents are kept fresh by
the GCS Data Connector, which re-syncs on its `data_connector_refresh_interval`
(default daily); call `make ingest` from CI (or locally) to force a sync. To
deploy the agent, use the ADK-native path — e.g. `uv run adk deploy cloud_run .`
or `adk deploy agent_engine` (not the deprecated agent-starter-pack tooling).

## Looking for vector search instead?
See [`rag-vector-search`](../rag-vector-search) for a Vector Search 2.0 variant,
or [`multiformat-hybrid-rag`](../../python/agents/multiformat-hybrid-rag) for a production hybrid
(semantic + keyword) system.
