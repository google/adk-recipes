# Smart Tool Router Agent (`contrib/python/smart-tool-router`)

Deployable ADK agent recipe demonstrating **session-stable semantic tool routing** and **principal-scoped tool result caching** with [`adk-smart-router`](https://pypi.org/project/adk-smart-router/) paired with ADK's built-in **[BigQuery Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/)** (`BigQueryAgentAnalyticsPlugin`) and `ContextCacheConfig`.

> **Looking for the conversational skill plugin (`plugin.json` + `SKILL.md`)?**
> See [`plugins/smart-router`](../../../plugins/smart-router/README.md).

## Architecture & How It Works

Agents with large tool catalogs (25 to 100+ tools) pay for every tool declaration on every turn, and naive per-turn dynamic tool filtering changes the prompt prefix mid-session, breaking Gemini implicit context caching.

This recipe wires `SmartRouterPlugin` immediately before `BigQueryAgentAnalyticsPlugin` on the ADK `App`:

1. **Session-locked Turn-1 routing (`SmartRouterPlugin`):** On Turn 1 of a session, the router embeds the user prompt with the configured Gemini Enterprise embedding model (`SMART_ROUTER_EMBEDDING_MODEL`, defaulting to `gemini-embedding-2`) and selects a canonical tool menu using either the zero-shot `PrototypeJudge` (cold start, including 1-hop upstream prerequisite rescue across `Returns:` $\to$ `Args:` links), a trained BigQuery ML `xgboost` model, or both in hybrid mode. That menu is locked for all subsequent turns in the session so Gemini implicit context caching stays active across any pinned Gemini model (`MODEL_NAME` in `.env.example`, defaulting to `gemini-3.7-flash` for controlled environments).
2. **Principal-scoped tool result caching (`cacheable_tools`):** Read-only lookup tools (`get_order_status`, `get_return_policy`, `get_loyalty_balance`) are cached in memory per `(app_name, user_id, tool_name, canonical_args)` with automatic cache invalidation whenever a mutating tool (`create_return_label`, `escalate_to_human`) runs.
3. **Closed-loop telemetry (`BigQueryAgentAnalyticsPlugin`):** `SmartRouterPlugin` stamps per-turn routing provenance (`decision_id`, `decision_source`, `kept_tools`, `confidence`, `replica_id`) onto `event.custom_metadata["smart_router"]`, which `BigQueryAgentAnalyticsPlugin` (`custom_metadata_allowlist=["smart_router"]`) streams into BigQuery `agent_events` for holdout A/B evaluation and BigQuery ML retraining.

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Google Cloud SDK (`gcloud auth application-default login`) when running with Gemini Enterprise / BigQuery enabled

## Quickstart & Local Setup

```bash
cd contrib/python/smart-tool-router
cp .env.example .env
# Edit .env and set GOOGLE_CLOUD_PROJECT=your-gcp-project-id
uv sync
```

### 1. Run Offline Qualification & Wiring Checks

Verify the agent's prompt-floor verdict, prerequisite argument reachability graph (`lookup_order_by_email` $\to$ `get_order_status` $\to$ `get_shipment_checkpoints`), and plugin wiring before enabling active routing:

```bash
uv run smart-router qualify --app app.agent:app
uv run smart-router doctor --app app.agent:app
```

### 2. Run Unit & Runnability Tests

```bash
uv run pytest
```

### 3. Run Local ADK Web & FastAPI Server

```bash
uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8080
# Or via ADK CLI:
uv run adk web . --port 8080
```

### 4. Enable Active Cold-Start Routing with BigQuery Agent Analytics

Update `.env`:

```bash
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
SMART_ROUTER_BQ_PROJECT=your-gcp-project-id
SMART_ROUTER_BQ_DATASET=smart_router
SMART_ROUTER_MODE=active
SMART_ROUTER_ROUTE_FRACTION=1.0
SMART_ROUTER_JUDGES=prototype
```

If your agent already logs events to BigQuery through `BigQueryAgentAnalyticsPlugin`, check whether your existing `agent_events` table is ready to train a BigQuery ML `xgboost` router:

```bash
uv run smart-router readiness --agent support_agent --project your-gcp-project-id --dataset smart_router
```

## License

Apache 2.0
