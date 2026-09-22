# RAG Agent — Agent Platform Search

## Intent

A clone-and-study RAG agent grounded on **Agent Platform Search** (Discovery
Engine). Drop documents in a GCS bucket and a managed Data Connector ingests,
chunks, embeds, and indexes them — the agent answers over that data store with
almost no ingestion code. The interesting part is **not** the agent (a thin
wrapper around one search tool); it's the **Terraform + data-connector
plumbing**.

## When To Use

- The user wants a RAG agent without building/maintaining an ingestion pipeline
  (the managed connector handles chunking + embeddings).
- Source documents are unstructured files (PDF/HTML/TXT) in Cloud Storage.
- The user wants a Discovery Engine grounding example with reproducible
  Terraform.

## Eval

- **Scenarios Path**: `tests/eval/datasets/` (config: `tests/eval/eval_config.yaml`)
- **Minimum Score**: not enforced — `custom_response_quality` graded 1–5 (aim ≥ 4)

## End-to-end flow

```
docs uploaded to GCS bucket
   -> GCS Data Connector (setUpDataConnectorV2, data_schema="content")
   -> auto-created Discovery Engine data store (managed ingest + embeddings)
   -> agent queries it at runtime via VertexAiSearchTool(data_store_id=data_store_path)
```

You never write chunking/embedding code: the Data Connector ingests and indexes
unstructured files for you. The price is a fiddly control plane (auto-generated
IDs, collection placement, LRO polling) — which is exactly what the Terraform
and the `scripts/` wrappers exist to tame.

## Most interesting files to study (in order)

### Terraform + data connector (the bulk of the logic)
1. **`infra/terraform/agent_platform_search.tf`** — the heart of the recipe.
   Shows the whole pattern: a `google_storage_bucket` for docs, a
   `null_resource.data_connector` whose create/destroy `local-exec`
   provisioners shell out to Python scripts, a `data "external" "data_store_id"`
   source that reads back the *auto-generated* data store, and a
   `google_discovery_engine_search_engine` built on top. Note how the search
   engine's `collection_id` and `data_store_ids` come from the `data.external`
   result, not hard-coded values.
2. **`infra/terraform/scripts/setup_data_connector.py`** — what the create
   provisioner runs. Idempotent (checks `getDataConnector` first), then calls
   `setUpDataConnectorV2` with a `collectionId`, an entity named `documents`,
   and schema-specific params (`content` → `CONTENT_REQUIRED`). Then polls the
   long-running operation. This is the canonical "create a managed GCS→Search
   ingestion connector via the REST API" recipe.
3. **`infra/terraform/scripts/get_data_store_id.py`** — solves the
   **auto-generated ID problem**. The connector creates a data store whose ID
   and *collection* you don't choose. This script reads the connector and parses
   the data store's full resource name to return BOTH `data_store_id` and
   `collection_id` as JSON, which Terraform consumes through the `data.external`
   data source. Study this together with file #1 to see the
   Terraform↔script handshake.
4. **`infra/terraform/datastore_outputs.tf`** — the bridge between infra and the
   running agent. Exposes `data_store_id`, `data_store_collection`, and a
   ready-to-paste `data_store_path`. You copy these into `.env` so the agent
   targets the right (auto-generated) data store.
5. **`infra/terraform/scripts/delete_data_connector.py`** — the destroy-time
   provisioner. Deletes the whole collection (and its data store) so
   `terraform destroy` is clean. Good example of wiring teardown into a
   `null_resource`'s `when = destroy` provisioner via `self.triggers`.
6. **`infra/terraform/scripts/start_connector_run.py`** — a standalone helper
   (not wired into Terraform) that triggers an immediate `ImportDocuments` run
   (`reconciliationMode: FULL`) and prints a console URL. Use it to force a sync
   instead of waiting for the periodic refresh.

### Terraform config & providers
7. **`infra/terraform/apis.tf`** & **`providers.tf`** — enables the required
   services (note `discoveryengine.googleapis.com` and the `vertex_sa` service
   identity) and declares providers. Note the `billing_override` aliased
   provider with `user_project_override = true`, used by the search engine for
   Discovery Engine quota attribution.
8. **`infra/terraform/variables.tf`** &
   **`agent_platform_search_variables.tf`** & **`vars/env.tfvars`** — the
   knobs: `project_id`, `project_name` (drives resource naming), `region`,
   `data_store_region` (default `global`), `data_connector_refresh_interval`,
   and `data_connector_data_schema`.

### Agent (thin layer — read last)
9. **`app/agent.py`** — how the runtime consumes the infra. Loads `.env`, builds
   `data_store_path` from `DATA_STORE_REGION` / `DATA_STORE_COLLECTION` /
   `DATA_STORE_ID` env vars and hands it to `create_search_tool`. The agent
   itself is a single `Agent` with one tool.
10. **`app/retrievers.py`** — `create_search_tool()` returns a real
    `VertexAiSearchTool` bound to the data store, or a mock function when
    `INTEGRATION_TEST=TRUE`. The seam that lets tests run without live Search.

## Data handling

- **Ingestion is fully managed.** The GCS Data Connector
  (`data_schema = "content"`) ingests unstructured files (PDF, HTML, TXT, …) —
  one document per file, IDs derived from the file URI — and Discovery Engine
  handles chunking/embeddings/indexing. No pipeline code in this recipe.
- **Docs bucket:** `${project_id}-${project_name}-docs`. Drop files here; the
  connector syncs them on its `data_connector_refresh_interval` (default daily,
  `86400s`), or run `start_connector_run.py` to sync immediately.
- **Other schemas** are supported by `setup_data_connector.py`
  (`document`/NDJSON, `csv`, `custom`) via the `--data-schema` flag /
  `data_connector_data_schema` variable.
- **Sample docs:** `sample_data/` ships a small fictional knowledge base;
  `make upload-sample-data && make ingest` loads it so you can query right away.

## Gotchas / things to know

- **The data store ID and collection are auto-generated.** After
  `make setup-infra`, copy the `data_store_id` and `data_store_collection`
  Terraform outputs into `.env` — the defaults in `.env.example` / `agent.py`
  may not match what the connector created.
- **The data store lives in `<project_name>-collection`, not
  `default_collection`.** That's why `get_data_store_id.py` resolves the real
  collection from the API instead of assuming a value.
- **`make setup-infra` shells out to `uv run` scripts** from Terraform
  provisioners, so `uv` must be installed on the machine running `terraform
  apply`.
- **Tests need credentials.** `tests/integration/test_agent.py` makes a live
  Gemini call and is skipped without ADC; the retriever is mocked via
  `INTEGRATION_TEST=TRUE` (set automatically by `make test`).
- **Provisioner runs are not auto-retried by Terraform** — connector setup polls
  its own LRO; re-running `apply` is safe because the script is idempotent.

## Where to run things

`Makefile` targets: `make setup-infra` (provision), `make install`,
`make playground` (local ADK web UI), `make test`, `make lint`.
Eval lives under `tests/eval/` (`eval_config.yaml` + `datasets/`).

## Reuse (copy as-is)

- **`infra/terraform/`** is self-contained (it provisions the datastore only) —
  copy the whole directory, **including `scripts/`**, into another project. Set
  `project_id` in `vars/env.tfvars` (and optionally `project_name`, which drives
  every resource name). The only host requirement is `uv` on the machine running
  `terraform apply` (provisioners call the PEP-723 scripts via `uv run`).
- There is **no code coupling to `app/`**: the agent reaches the datastore purely
  through the `DATA_STORE_REGION` / `DATA_STORE_COLLECTION` / `DATA_STORE_ID` env
  vars, which you populate from the Terraform outputs.
