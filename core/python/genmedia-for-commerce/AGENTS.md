# GenMedia for Commerce — Full-Stack Multi-Agent Retail Media

## Intent

A clone-and-study **full-stack, multi-agent** ADK app that generates retail
media — Virtual Try-On (**VTO**) images/videos, 360° product spins
(**Reference-to-Video / R2V**), product fitting on a virtual model, and
background swaps — by orchestrating **Gemini** and **Veo 3.1** through
multi-stage media pipelines. The interesting part is **not** the router agent
(a thin ADK wrapper around one **MCP toolset**); it's the **MCP tool server +
workflow pipelines** (framing → parallel Veo generation → automated
validation/retry), the **in-memory catalogue vector search**, and the **full app
stack** (FastAPI + React + Terraform + Agent Engine / Gemini Enterprise
registration).

## When To Use

- The user wants an **orchestrator agent** that drives multi-stage
  media-generation pipelines (framing → generation → validation/retry) instead
  of a single tool call.
- The user wants a reference for exposing generation workflows as **MCP tools**
  and wiring them into an ADK agent, a **FastAPI** REST surface, and a **React**
  frontend.
- The user needs a **Veo/Gemini retail-media** example with automated quality
  validation (rotation-consistency + glitch detection) and ADK eval sets.

## Eval

- **Scenarios Path**: `tests/eval/evalsets/` (config: `tests/eval/eval_config.json`)
- **Minimum Score**: two weighted criteria, each `threshold: 0.5` —
  `tool_trajectory` (`weight 0.6`, `tool_name_match: flexible`) +
  `response_match_v2` (`weight 0.4`); `num_runs: 1`.
- Run with `make eval` (default `basic.evalset.json`) or `make eval-all` (loops
  every `tests/eval/evalsets/*.evalset.json`). Both call `adk eval`. Cases mix
  **tool-trajectory** checks (e.g. `route_product_fitting` asserts a
  `product_fitting` call with `gender`/`ethnicity` args) with **reference**
  behavior checks (e.g. `ask_for_missing_params`).

## End-to-end flow

```
user (React frontend / ADK web / Gemini Enterprise)
   -> root_agent "genmedia_router" (genmedia4commerce/agent.py)
      - before_model_callback: uploads described by Gemini, pushed to GCS,
        inline bytes replaced with [user_upload | filename | description]
   -> McpToolset -> GenMedia MCP server (mcp_server/server.py, 7 tools)
      - before_tool_callback resolves filenames -> base64 from GCS
   -> media workflow pipeline (workflows/<capability>/):
        framing/canvas -> parallel Veo 3.1 / Gemini generation
        -> automated eval + (spinning) rotation/glitch validation & retry
   -> after_tool_callback: generated media saved as ADK artifacts + GCS,
        LLM sees only a text summary (no base64 blobs in context)
   -> frontend renders artifacts; history persisted to GCS per session
```

## Most interesting files to study (in order)

### MCP tool server (the tool surface)
1. **`genmedia4commerce/mcp_server/server.py`** — a `FastMCP` server
   (`genmedia-retail`) exposing **7 tools**: `product_fitting`, `image_vto`,
   `video_vto`, `background_changer`, `product_spinning`, `animate_model`,
   `catalog_search`. Each `@server.tool()` is a thin async wrapper that
   JSON-encodes the result of a `run_*` workflow. Runs over **stdio** (for the
   ADK agent subprocess) or **SSE** (`--transport sse`, for external clients).

### Media-generation workflows (the bulk of the logic)
2. **`genmedia4commerce/workflows/`** — the real pipelines, grouped by
   capability: `video_vto/` (clothes catwalk + glasses), `image_vto/` (static
   clothes/glasses VTO with evaluation ranking), `spinning/` (shoe & product
   360° R2V + interpolation), `product_enrichment/product_fitting/` (front/back
   views on a model body), `other/background_changer/`, and `shared/`
   (Gemini/Veo/image/video/GCS utilities). Each `*/pipeline.py` is the entry the
   matching `*_mcp.py` calls.
3. **`genmedia4commerce/workflows/spinning/r2v/shoes/pipeline.py`** — the richest
   pipeline: classify shoe angle/closure → select & stack images (Veo caps
   reference images at 3) → generate → **validate**. Study its two validators:
   **`video_validation_r2v.py`** (`validate_and_fix_product_spin_consistency_r2v`,
   rotation-direction + glitch checks with retry) and
   **`product_consistency_validation.py`** (`validate_product_consistency`).
4. **`genmedia4commerce/workflows/shared/vector_search.py`** — the catalogue
   backend: an **in-memory dot-product search** over pre-computed embeddings
   (`numpy`) + `metadata.parquet` (`pyarrow`), **Matryoshka-truncated to 128d**,
   with `audience`/`season`/etc. filters. No managed index — assets are pulled
   from GCS at import. Read side of `catalog_search`.
5. **`genmedia4commerce/workflows/shared/`** — `veo_utils.py` (Veo R2V calls),
   `image_utils.py` (framing, canvas, background removal), `video_utils.py`,
   `llm_utils.py`, `gcs_utils.py`, `person_eval.py` — the reusable primitives
   every pipeline composes.

### App stack & infrastructure
6. **`genmedia4commerce/fast_api_app.py`** (+ **`chat_api.py`**) — the combined
   server: `get_fast_api_app` (ADK `/run`, sessions, optional web UI) **plus**
   every `mcp_server/*/*_api.py` REST router, a `/feedback` + `/health` +
   `/api/status` endpoint, an **embedded MCP server on a daemon thread (SSE)**,
   and SPA/static mounts for the built React app. This is the Cloud Run
   entrypoint.
7. **`genmedia4commerce/agent_engine_app.py`** (+ **`genmedia4commerce/app_utils/deploy.py`**) —
   the Agent Engine path: wraps `agent.app` in an `AdkApp` subclass
   (`AgentEngineApp`) with telemetry, GCS artifacts, and a `register_feedback`
   op. `make deploy-agent-engine` exports requirements and calls
   `genmedia4commerce.app_utils.deploy`.
8. **`infra/terraform/`** — GCP provisioning: `main.tf` (APIs), `storage.tf`
   (media bucket `${project_id}-genmedia-for-commerce-media-payloads`),
   `artifact_registry.tf`, `cloudrun.tf`, `iam.tf`. **`infra/model_training/`** —
   optional Gemini **LoRA** shoe-side classifier fine-tune/eval
   (`make train-shoe-model` / `make eval-set-shoe-model`).

### Agent + frontend (thin layer — read last)
9. **`genmedia4commerce/agent.py`** — `root_agent` (`genmedia_router`, Gemini)
   and `app`. Thin on routing (one `McpToolset`), but carries the recipe's
   **callback machinery**: `before_model` (GE-stable session id, upload →
   describe → GCS), `after_model` (inject `[session_id=...]`), `before_tool`
   (filename → base64), `after_tool` (media → artifacts + GCS, summary to LLM).
10. **`genmedia4commerce/agent_utils.py`** — the helpers behind those callbacks:
    **GCS-backed conversation history** (`append_to_history` /
    `retrieve_from_history`), `upload_asset_to_gcs` / `copy_gcs_asset`,
    `resolve_filename_to_gcs_uri`, `extract_media` / `resolve_media`, and
    parallel image description.
11. **`genmedia4commerce/agents/style_advisor_agent/agent.py`** — a second,
    self-contained **multi-agent** app: a `style_advisor` searcher that delegates
    to a `stylish_agent` curator sub-agent (`sub_agents=[...]`). Its own
    `App(...)`; not wired into the router. Read for the ADK sub-agent pattern.
12. **`frontend/`** — the **React + Vite** UI (`genmedia-studio-frontend`), with
    per-capability views (`image_vto/`, `video_vto/`, `spinning/`,
    `product_enrichment/`). Talks to the FastAPI REST + ADK API; built and
    served by `fast_api_app.py` in production.

## Data handling

- **Session state & history are GCS-backed**, not in ADK memory. All media +
  `history.json` live under `sessions/<global_session_id>/` in
  `MEDIA_BUCKET = ${project_id}-genmedia-for-commerce-media-payloads`
  (`user_uploads/`, `generated_assets/`). A **stable global session id** is
  injected as `[session_id=...]` so the Gemini Enterprise playground (which
  rotates session ids each turn) keeps a consistent storage key.
- **Base64 never enters LLM context.** Uploads are described by Gemini and
  replaced with `[user_upload | filename | description]`; tool outputs are saved
  as artifacts + GCS and reduced to a text summary. Filenames follow
  `TIMESTAMP_TYPE_INDEX.EXT` (`_u_` upload, `_c_` catalog, `_g_` generated);
  `before_tool` resolves them back to bytes from GCS.
- **Catalogue = in-memory vector search.** `workflows/shared/vector_search.py`
  loads pre-computed embeddings + `metadata.parquet` into RAM and does
  dot-product similarity (128d Matryoshka slice) with metadata filters. Assets
  auto-download at import from a hash-named GCS bucket via
  `config.pull_assets()` (`make setup-infra` runs `pull-assets` first).
- **Model names are env-driven** (`MODEL_NAME_GENERATED_*`, `EMBEDDING_MODEL`,
  `MULTIMODAL_EMBEDDING_MODEL`); the router model is `config.agent_model`
  (`MODEL_NAME_GENERATED_1`).

## Gotchas / things to know

- **`config.env` vs `.env`.** The runtime loads the **package-internal**
  `genmedia4commerce/config.env` (a gitignored build artifact). The root
  `config.env` is the source of truth — `make sync-config` copies it in, and
  most targets depend on it. Copy `config.env.example` → `config.env` first.
- **`.env.example` model literals are deprecated.** `MODEL_NAME_GENERATED_*`
  captures the original recipe (`gemini-3.6-flash`, …). Per the repo `AGENTS.md`
  model policy, review/replace with `gemini-3.6-flash` before use.
- **The `eval` extra is intentionally empty.** `google-adk[eval]` /
  `aiplatform[evaluation]` cap `scikit-learn<=1.5.2`, which conflicts with the
  image code's `scikit-learn>=1.7.2`. `make eval` works via the base
  `google-cloud-aiplatform[...,evaluation]` dependency; install `google-adk[eval]`
  separately only if you need those extras.
- **Heavy dependency set** (OpenCV, moviepy, rembg, insightface, onnxruntime,
  scikit-image). `uv sync` is slow and pulls large ML wheels; `make install`
  also installs `g++`/build tools.
- **Live GCP is required at runtime** (Vertex AI / Veo / Gemini, GCS). Config
  probes Vertex at import (`test_vertex_connection`). The runnability test
  (`tests/test_runnability.py`) patches `google.auth.default` and only asserts
  that `root_agent`/`app` import and are non-None.
- **MCP transport is dual.** The ADK agent connects over **SSE** when
  `MCP_SERVER_URL` is set, else spawns the server as a **stdio subprocess**.
  In Cloud Run the server also runs embedded on an SSE daemon thread inside
  `fast_api_app.py`.
- **Deployment is multi-step, two targets.** `deploy-agent-engine` ships the
  **agent only** (no REST/VTO endpoints); `deploy-cloudrun` ships the **full
  stack**. Both need `make setup-infra` first.

## Where to run things

`Makefile` targets:

- **Setup**: `make install`, `make sync-config`, `make setup-infra` (Terraform +
  `pull-assets`), `make tf-plan`, `make tf-destroy`.
- **Local**: `make playground` (ADK web UI), `make dev` (backend + frontend),
  `make run-backend`, `make run-frontend`, `make mcp-server` (standalone SSE).
- **Quality**: `make test` (unit + integration), `make eval` /
  `make eval-all`, `make lint` (codespell + ruff + ty).
- **Deploy**: `make deploy-agent-engine`, `make deploy-cloudrun`,
  `make register-gemini-enterprise`.
- **Shoe classifier (optional)**: `make train-shoe-model`,
  `make eval-set-shoe-model`.

Eval lives under `tests/eval/` (`eval_config.json` + `evalsets/`).

## Reuse (copy as-is)

- **`genmedia4commerce/mcp_server/`** is a self-contained **FastMCP** server —
  run it standalone (`python -m mcp_server.server` / `make mcp-server`) and
  connect any MCP client. It imports `genmedia4commerce/workflows/`, so copy the
  two together (the workflows are the actual generation logic).
- **`infra/terraform/`** is self-contained GCP provisioning (media bucket, AR
  repo, Cloud Run service, IAM, APIs). `make setup-infra` derives `TF_VAR_*`
  from `config.env`; set `PROJECT_ID` (and Cloud Run knobs) there.
- **`frontend/`** is a standalone **React + Vite** app with its own
  `package.json` — `npm install && npm run build` and point it at the backend's
  REST + ADK API.
- There is **no hard coupling** from the agent to the workflows: `agent.py`
  reaches every capability purely through the **MCP toolset** (stdio subprocess
  or SSE via `MCP_SERVER_URL`).
