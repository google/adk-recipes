---
name: adk-tokenomics
description: >
  Use when instrumenting Google ADK agents for turn-level token and USD cost
  telemetry, profiling session traces or `adk eval` artifacts, launching the
  Token Control Tower web observatory, optimizing context caching, event
  compaction, thinking budgets, sub-agent model tiering, or modular skills, or
  enforcing shift-left CI/CD financial and token budget gates via `agents-cli`.
---

# ADK Tokenomics & FinOps Control Tower (`adk-tokenomics`)

This project has the `adk-tokenomics` extension installed for `agents-cli`. It decomposes every Google ADK LLM turn into **4 cost buckets** (**Fresh Input**, **Cached Prefix**, **Visible Output**, and **Reasoning / Thinking**), computes rate-card USD spend and counterfactual savings, diagnoses **5 FinOps anti-patterns**, and enforces deterministic **pass/fail CI/CD budget gates**.

---

## 1. Extension CLI Commands (`agents-cli`)

All arguments after the command name pass straight through to the Token Control Tower CLI, **except `--help`**, which `agents-cli` answers itself. Use `-h` to see the tool's flags (e.g. `agents-cli tokenomics profile -h`, `agents-cli eval budget -h`). Relative paths resolve from the directory you run the command in.

```bash
# 1. Profile turn-by-turn token waterfall & 5 FinOps optimization rules
# Given a directory, profile analyzes the MOST RECENT session file (it says so on stderr);
# pass -t <file> for a specific session, or use `eval budget` for an aggregate across all.
agents-cli tokenomics profile -t artifacts/traces/
agents-cli tokenomics profile -t artifacts/traces/ --format json

# Profile a live BigQuery session directly
agents-cli tokenomics profile -s <session_id> --project <gcp-project> --dataset bq_adk_ds

# 2. Enforce shift-left CI/CD unit-economics gates (exits 0 on pass, 1 on breach/missing traces)
# Works on both ADKTokenomicsPlugin JSON traces and `.adk/eval_history/*.evalset_result.json`
agents-cli eval budget \
  --traces . \
  --max-cost-per-turn 0.005 \
  --min-cache-rate 0.60 \
  --max-tokens-per-turn 5000 \
  --max-latency-ms 15000 \
  --format json

# Equivalent via the top-level tokenomics command:
agents-cli tokenomics assert --traces artifacts/traces/ --max-cost-per-turn 0.005

# 3. Launch the interactive Material 3 Dark Slate Web Observatory (default localhost:8080)
agents-cli tokenomics web --traces artifacts/traces/ --port 8080 --no-browser
```

> **Cache-rate gate:** `--min-cache-rate` only makes sense once prompts are large enough to be cached (context caching starts at about 1,024 prompt tokens). Agents with short prompts report a 0% cache-hit rate and will fail this gate, so leave `--min-cache-rate` out until your static prefix crosses that size.

> **Fail-Closed CI Note:** `agents-cli eval budget` (and `agents-cli tokenomics assert`) fails closed (`exit 1`) if trace artifacts are missing, empty (`0` turns), or lack token usage metadata. Pass `--allow-empty` only when optional smoke suites intentionally emit zero LLM turns.

> **What `--traces` accepts:** a single trace file, a folder, or `.` (the project root, which also searches `artifacts/traces/` and `.adk/eval_history/`); with no `--traces`, `artifacts/traces/` is used. Output is human-readable text by default; add `--format json` for machine-readable results. `agents-cli eval generate` writes a `traces_<timestamp>.json` next to the plugin's `<session_id>.json` files; it has no token usage, so it's skipped (and reported as skipped) whenever plugin session traces are present, and the gate fails closed if it's the only input. `profile` on such a file alone shows character-count **estimates**, clearly labeled.

---

## 2. Instrumenting a Google ADK Agent (`app/agent.py`)

To capture live per-turn sub-agent attribution, workflow stages (`Root`, `AgentTool`, `Parallel`, `Loop`, `Sequential`), tool calls, and OpenTelemetry `gen_ai.client.cost.usd` histograms at runtime:

1. **Add the Python SDK to the agent project:**
   Install from the copy that `agents-cli extension add` already placed in the project, so the SDK
   always matches the `agents-cli tokenomics` commands. This needs no PyPI or GitHub access.
   ```bash
   uv add --no-workspace ./extensions/adk-tokenomics
   # Optional: include BigQuery streaming sink dependencies
   uv add --no-workspace ./extensions/adk-tokenomics --extra bigquery
   ```
   > **Local development and `agents-cli eval`:** `agents-cli deploy` leaves `/extensions/`
   > out of the upload (see the project's `.gcloudignore`), so if you explicitly want runtime
   > telemetry in a deployed build as well, install from GitHub instead:
   > `uv add "adk-tokenomics-plugin @ git+https://github.com/google/adk-recipes.git@adk-tokenomics"`.
   > **Uninstall order (for `./extensions/adk-tokenomics`):** run `uv remove adk-tokenomics-plugin` *before* `agents-cli extension remove`.
   > Otherwise the project's dependencies point at a missing folder and `uv run` / `agents-cli install` fail.

2. **Attach `ADKTokenomicsPlugin` to the ADK `App`:**
   Registering on `App(plugins=[...])` automatically propagates callbacks across the entire `root_agent` and `sub_agents` hierarchy.
   Keep it **opt-in for local development and evals**, so production images never need the package:
   ```python
   import os

   from dotenv import load_dotenv
   from google.adk.apps import App

   load_dotenv()  # agents-cli imports agent.py before it reads .env
   plugins = []
   if os.getenv("TOKENOMICS_ENABLED") == "1":
       from adk_tokenomics_plugin import ADKTokenomicsPlugin

       plugins.append(
           ADKTokenomicsPlugin(
               app_name="my_agent",
               auto_export_path="artifacts/traces/live_adk_session.json",
           )
       )

   app = App(name="my_agent", root_agent=root_agent, plugins=plugins)
   ```
   Turn it on locally by adding `TOKENOMICS_ENABLED=1` to the project `.env`, or by exporting it in your shell.
   `.env` is git-ignored and isn't uploaded on deploy, so the plugin stays off in production.
   `python-dotenv` already ships with `google-adk`, so the import needs no extra dependency. `app_name` is only the label recorded on every turn (and BigQuery row); using your `App(name=...)` keeps them aligned, but it doesn't have to match.
   Finished sessions archive automatically to `artifacts/traces/<session_id>.json` (and `await plugin.close()` flushes any queued BigQuery batches on shutdown).
   `auto_export_path` (`live_adk_session.json`) is only the **working file** for the session in progress. When a new session starts or the process exits, it's archived as `<session_id>.json`, so it's normally absent after a run. Its folder decides where archives go, so always point `profile`, `eval budget`, and `web` at the `artifacts/traces/` directory, not the live file. A relative `auto_export_path` resolves from the agent process's working directory (the project root for `agents-cli eval`, `agents-cli playground`, and `adk web` run from the project); use an absolute path if you launch the agent from somewhere else.

3. **Generate traces, then gate them:** any run of the instrumented agent records sessions, e.g. `agents-cli eval generate` (runs the project's eval dataset), `agents-cli playground`, or `adk web`. Then run `agents-cli eval budget --traces . --max-cost-per-turn 0.005` and `agents-cli tokenomics profile -t artifacts/traces/`.

---

## 3. Remediating the 5 FinOps Profiler Recommendations

When `agents-cli tokenomics profile` flags an optimization opportunity, apply the corresponding Google ADK configuration:

| Profiler Rule | Unoptimized ADK Anti-Pattern | Target Google ADK Fix |
| :--- | :--- | :--- |
| **`model_tiering`** | Frontier Pro model used on simple deterministic routers or fetchers (`thinking_tokens == 0`, output `<= 600` tokens) | Set lightweight routing/extraction `LlmAgent(model="gemini-2.5-flash")` (or `gemini-3.8-flash`); reserve Pro models for synthesis/planning sub-agents |
| **`context_caching`** | `cached_tokens == 0` with `fresh_tok > 1024` (`App` omits cache config) | Pass `context_cache_config=ContextCacheConfig(min_tokens=1024, ttl_seconds=300)` to `App(...)` (`from google.adk.agents.context_cache_config import ContextCacheConfig`) |
| **`history_compaction`** | `>= 4` user turns in a session or `+350` fresh-token per-turn growth | Pass `events_compaction_config=EventsCompactionConfig(compaction_interval=4, overlap_size=1)` to `App(...)` (`from google.adk.apps.app import EventsCompactionConfig`) |
| **`modular_skills`** | `> 5,000` fresh input tokens caused by monolithic static playbooks in `instruction` | Keep `LlmAgent(instruction=...)` concise and move reference playbooks into on-demand `SkillToolset` (`SKILL.md`) modules loaded via `load_skill` |
| **`thinking_budget`** | `thinking_tokens > 256` and `thinking / output > 0.80x` | Bound reasoning via `planner=BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget=256))` (or `thinking_budget=0` on deterministic routers) |

---

## 4. Python Unit Test & Pytest Budget Gates

For in-process `pytest` or `unittest` suites alongside `agents-cli eval budget`:

```python
from adk_tokenomics_plugin import (
    TokenomicsTestCase,
    assert_cache_ratio_above,
    assert_cost_under,
    assert_thinking_budget,
    assert_token_budget,
)


def test_turn_economics(tracker):
    assert_cost_under(tracker, max_dollars=0.01)
    assert_cache_ratio_above(tracker, min_ratio_pct=60.0)  # 0.0-100.0% in Python; 0.0-1.0 on CLI --min-cache-rate
    assert_token_budget(tracker, max_tokens=8000)
    assert_thinking_budget(tracker, min_tokens=0, max_tokens=512)
```

---

## 5. Rate Cards & Environment Variables

- **Custom Rate Cards:** Edit pricing interactively in `agents-cli tokenomics web` (`Rate Card` modal), place a project-local `./models_config.json`, or set `$TOKENOMICS_MODELS_CONFIG_PATH`.
- **BigQuery Telemetry:** Set `GOOGLE_CLOUD_PROJECT` and `BQ_ANALYTICS_DATASET_ID` (defaults to `bq_adk_ds`, table `adk_agent_events`, view `v_llm_response`).
