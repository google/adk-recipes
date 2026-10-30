---
name: adk-smart-router
description: >-
  End-to-end playbook for qualifying, wiring, configuring, and operating
  adk-smart-router on Google ADK agents — covering offline prompt qualification,
  prerequisite argument-graph auditing, Day-0 PrototypeJudge cold-start routing,
  Day-1 BigQuery ML XGBoost + Hybrid routing, startup warmup, and principal-scoped
  tool result caching.
metadata:
  author: Mike Henderson
  license: Apache-2.0
  version: 1.0.1
---

# `adk-smart-router` Agentic Integration Playbook

Use this skill whenever a user asks to evaluate, integrate, configure, train, or troubleshoot [`adk-smart-router`](https://github.com/statmike/adk-smart-router) on an [ADK](https://adk.dev/) (`google-adk`) agent.

Follow the steps below in order. **Never wire or enable routing before running Step 1 (`smart-router qualify`).**

---

## Step 1: Offline Qualification & Prerequisite Graph Audit

Before provisioning any Google Cloud infrastructure or editing the user's `App`, install `adk-smart-router` and run `smart-router qualify` against their `App` or `LlmAgent`:

```bash
uv add adk-smart-router
uv run smart-router qualify --app <module.path>:<app_or_agent_attr>
```

### 1.1 Interpreting the Prompt-Floor Verdict
`qualify` measures the agent's real tool declarations and system instruction against the model's context-cache floor (`floor + 512` tokens of retention headroom):

- **`PAYS` ($\ge 25\%$ steady-state saving):** Proceed to Step 2. The pruned prompt stays safely above the context-cache floor.
- **`THIN` ($< 25\%$ saving, or cross-session prefix drops below floor):** Proceed with caution. If the cross-session prefix drops under the floor, Turn 1 opens cold across sessions while Turns 2+ still cache within the session. You can raise `max_tools_kept` (default `11`) if Turn-1 cache warmth matters more than maximum pruning.
- **`UNCACHED` (full prompt is already below the cache floor):** Every removed token saves at the full uncached rate (`1.0x`), though absolute token savings are smaller.
- **`TRAP` (pruning drops a currently cached prompt below the cache floor):** **STOP. Do not enable tool pruning (`mode="active"`) with default menu width.** Losing the within-session context cache causes kept tokens to bill at `10x` their cached rate (`+25.5%` measured whole-run cost increase). To remediate:
  1. Increase `max_tools_kept` in `RouterConfig` until the pruned prompt clears the `retained` token floor reported by `qualify`, **or**
  2. Keep tool pruning disabled (`min_tools_to_route=9999`) and use `SmartRouterPlugin` strictly for principal-scoped tool result caching (`cacheable_tools`).

### 1.2 Interpreting the Argument-Reachability Audit
`qualify` also builds a static dependency graph (`argument_graph`) across all declared tools by matching `Returns:` docstring descriptions against `Args:` parameter names and descriptions:
- **Entry tools:** Tools whose required arguments are either natural-language values (`query`, `reason`, `description`) or identifiers users typically hold at the start of a session (`order_id`, `email`).
- **Downstream tools:** Tools that require an internal identifier (`customer_id`, `shipment_id`, `invoice_id`) produced by an upstream lookup tool's `Returns:` section.
- **Unreachable / Stranded tools:** If `qualify` reports tools that require an identifier no other tool claims to return:
  1. **Fix the upstream tool's docstring** by adding a `Returns:` section naming the identifier it produces (this also enables `PrototypeJudge`'s 1-hop upstream prerequisite propagation at runtime!), **or**
  2. Generate the argument-marking template with `--write-arguments args.txt`, mark each line as `held` (assigned by your system) or `supplied` (held by the user on Turn 1), and pass `--arguments args.txt` to `qualify` and later verify against real traffic with:
     ```bash
     uv run smart-router qualify --app <module>:<attr> --write-arguments args.txt
     # Edit args.txt (e.g., `sku held`, `order_id supplied`), then re-run:
     uv run smart-router qualify --app <module>:<attr> --arguments args.txt
     uv run smart-router check-arguments --app <module>:<attr> --arguments args.txt --project my-gcp-project
     ```

---

## Step 2: Audit the Tool Catalog for Docstring Quality & Result Caching

Before wiring `SmartRouterPlugin`, inspect the agent's tool functions:

1. **Docstring check for `PrototypeJudge` & `argument_graph`:**
   - Ensure every tool has a clear summary line, an `Args:` block describing each parameter, and a `Returns:` block naming any IDs or keys returned in its response payload.
   - Why this matters: `PrototypeJudge` embeds the function name, docstring, and parameter schema (`tool_text`), and propagates `0.7x` of each downstream tool's similarity score one hop upstream across the `Returns:` $\to$ `Args:` graph so prerequisite lookup tools are automatically rescued.
2. **Identify `cacheable_tools` and `never_prune`:**
   - **`cacheable_tools: Mapping[str, int]`** — Select **read-only, deterministic** lookup tools (e.g., `get_return_policy`, `get_order_status`, `get_account_tier`) and assign a TTL in seconds (`30` to `3600`). Never include mutating tools or tools with side effects.
   - **Automatic read-your-own-writes invalidation:** Any tool *not* listed in `cacheable_tools` is treated as a potential writer—whenever it executes, `CacheHooks` automatically purges all cached entries for that `(app_name, user_id)` principal (`cache.purge_user(scope)`). Furthermore, if a tool listed in `cacheable_tools` ever mutates `tool_context.actions`, it is permanently blocklisted at runtime and triggers a principal purge.
   - **`never_prune: frozenset[str]`** — List any critical safety or escalation tools (e.g., `frozenset({"escalate_to_human"})`) that must remain declared on every routed session regardless of model scores.

---

## Step 3: Wire `SmartRouterPlugin`, Startup `warmup()`, and Verify with `doctor`

### 3.1 Plugin Registration Order (Load-Bearing)
`SmartRouterPlugin` **must** be registered in `App(plugins=[...])` **before** [BigQueryAgentAnalyticsPlugin](https://adk.dev/integrations/bigquery-agent-analytics/):
1. In `before_model_callback`, `SmartRouterPlugin` prunes `llm_request.tools_dict` and `llm_request.config.tools` first so `BigQueryAgentAnalyticsPlugin` logs the actual menu sent to the model.
2. In `on_event_callback`, `SmartRouterPlugin` stamps `event.custom_metadata["smart_router"]` first so `BigQueryAgentAnalyticsPlugin` writes the routing decision into BigQuery `attributes.custom_metadata`.
3. `BigQueryAgentAnalyticsPlugin` **must** include `custom_metadata_allowlist=["smart_router"]`.

### 3.2 Complete Wiring Pattern (including Startup `warmup`)
Always call `await plugin.warmup(app)` during service startup (e.g., FastAPI `lifespan` or before starting the runner loop). `warmup(app)` pays the ~1.9s cold-start connection cost of the Gemini Enterprise `gemini-embedding-2` client and pre-embeds all tool prototypes once before user traffic arrives, preventing Turn 1 of the first live session from hitting the `600 ms` hot-path embedding timeout:

```python
from google.adk.agents import LlmAgent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from adk_smart_router import RouterConfig, SmartRouterPlugin, load_config

router_plugin = SmartRouterPlugin(
    load_config(
        cacheable_tools={
            "get_order_status": 300,
            "get_return_policy": 3600,
        },
        never_prune=frozenset({"escalate_to_human"}),
    )
)

analytics_plugin = BigQueryAgentAnalyticsPlugin(
    project_id="my-gcp-project",
    dataset_id="smart_router",
    custom_metadata_allowlist=["smart_router"],
)

app = App(
    name="support_app",
    root_agent=LlmAgent(
        name="support",
        model="gemini-3.7-flash",
        instruction="Resolve customer requests using your tools.",
        tools=ALL_TOOLS,
    ),
    context_cache_config=ContextCacheConfig(min_tokens=2048, ttl_seconds=1800),
    plugins=[router_plugin, analytics_plugin],
)

# In an async server lifespan or entrypoint, pre-warm before serving traffic:
# await router_plugin.warmup(app)
```

### 3.3 Verify Wiring with `smart-router doctor`
Run `doctor` and confirm it reports `3 of 3` (or `4 of 4` when active):

```bash
uv run smart-router doctor --app <module.path>:app
```

---

## Step 4: Choose a Judge Tier & Graduate from Day-0 to Day-1

### Tier 1 — Day-0 Cold Start (`SMART_ROUTER_JUDGES=prototype`)
Use immediately on Day 0 when the agent has zero logged BigQuery traffic and no trained model in GCS:
```bash
export SMART_ROUTER_MODE=active
export SMART_ROUTER_ROUTE_FRACTION=0.10
export SMART_ROUTER_HOLDOUT_FRACTION=0.05
export SMART_ROUTER_BQ_PROJECT="my-gcp-project"
export SMART_ROUTER_JUDGES="prototype"
# Optional: snap PrototypeJudge scores to 24 zero-data canonical menus for cross-session cache sharing:
export SMART_ROUTER_COLD_START_MENU_COUNT=24
```

### Tier 2 — Check Training Readiness (`smart-router readiness`)
Once `BigQueryAgentAnalyticsPlugin` has logged traffic (or if the agent was already logging before installing `adk-smart-router`), check if the corpus is ready to train:
```bash
uv run smart-router readiness --agent support --project my-gcp-project --dataset smart_router
```

### Tier 3 — Train, Tune, Publish, and Switch to Hybrid (`SMART_ROUTER_JUDGES=xgboost,prototype`)
When `readiness` passes ($\ge 1,000$ qualifying sessions):
1. **Provision BigQuery dataset, remote connection, and GCS bucket (once from a clone of `statmike/adk-smart-router`):**
   ```bash
   ./scripts/provision.sh
   ```
2. **Run the 5-stage BigQuery ML training procedure (`sql/train_router.sql`) and publish to GCS (`scripts/publish_router.py`):**
   - `publish_router.py` exports the BQML boosted-tree classifier to local XGBoost `model.ubj`, extracts canonical tool menus via greedy weighted max-coverage, checks 5 promotion gates (`G1`–`G5`), and writes the immutable generation to `gs://<bucket>/routers/<agent>/<version>/`.
3. **(Recommended) Switch runtime to Hybrid (`xgboost,prototype`):**
   ```bash
   export SMART_ROUTER_GCS_BUCKET="my-router-bucket"
   export SMART_ROUTER_JUDGES="xgboost,prototype"
   export SMART_ROUTER_PROTOTYPE_WEIGHT=0.35
   ```
   In hybrid mode, `xgboost` and `PrototypeJudge` share the single Turn-1 prompt embedding (`~3.3 ms` extra local compute). `xgboost` scores tools known at training time while `PrototypeJudge` semantically scores any newly deployed (`classless`) tools added after the last retrain—pruning irrelevant new tools and rescuing relevant ones (`+25.5` points of complete-session recovery on held-out tools).
4. **Monitor live savings and drift:**
   ```bash
   uv run smart-router status --agent support
   ```

---

## Step 5: Critical Guardrails & Troubleshooting Checklist

1. **Never change `vertex_location` away from `"global"` (or `"us"`):**
   - `gemini-embedding-2` is published on `"global"` and `"us"`, **not** on regional endpoints like `us-central1` or `us-east4`. Setting `SMART_ROUTER_VERTEX_LOCATION=us-central1` causes `404 Not Found` on every embedding call, tripping the circuit breaker and silently failing open to passthrough.
2. **Why a session was not pruned (`decision_source` in BigQuery `attributes.custom_metadata.smart_router`):**
   - `"fresh"` / `"reused"`: The router acted—a canonical menu was chosen on Turn 1 (`"fresh"`) and reused on subsequent turns (`"reused"`).
   - `"holdout"`: The session is in the permanent unrouted control arm (`holdout_fraction=0.05`).
   - `"small"`: The agent declared fewer than `min_tools_to_route` (`20`) tools.
   - `"floor"` or `"watch"`: Pruning would drop the prompt below `MIN_CACHE_TOKENS` (`"floor"`), or `CacheWatch` observed live context-cache hit degradation on routed sessions relative to holdout and tripped the automatic circuit breaker (`"watch"`).
   - `"cheap"` or `"lowconf"`: Expected token savings did not clear the miss-penalty decline rule (`"cheap"`), or every tool scored below `min_top_prob` (`"lowconf"`), so the router kept the full tool catalog.
   - `"timeout"`, `"error"`, or `"breaker"`: Turn-1 embedding timed out, returned an API error, or was skipped while the embedding circuit breaker was open.
3. **Multi-agent (`sub_agents`) behavior:**
   - `transfer_to_agent` is in `FRAMEWORK_TOOLS` and is never pruned.
   - Routing decisions are keyed by `(session.id, agent_name)`, so a parent orchestrator's menu never bleeds into a sub-agent's tool list.

---

## CLI Quick Reference

| Command | Purpose |
| --- | --- |
| `smart-router qualify --app MODULE:ATTR` | Offline prompt-size (`PAYS`/`THIN`/`UNCACHED`/`TRAP`) and argument-reachability check |
| `smart-router readiness --agent NAME --project ID` | Check if existing BigQuery Agent Analytics logs can train a model today |
| `smart-router doctor --app MODULE:ATTR` | Verify plugin registration order, `custom_metadata_allowlist`, `ContextCacheConfig`, and active judge setup |
| `smart-router check-arguments --app MODULE:ATTR --arguments FILE` | Audit held/supplied argument markings against logged first-turn user messages |
| `smart-router status --agent NAME` | Inspect live artifact, 7-day G1 billed-token delta vs. holdout, feature drift, and retrain ledger |
