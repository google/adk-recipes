# ⚡ ADK Tokenomics Plugin & Control Tower

> **Turn-Level AI Unit Economics, Context Profiling, Automated Model Tiering, OpenTelemetry FinOps & CI/CD Budget Gating for Google ADK (Agent Development Kit).**

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-BasePlugin-brightgreen)](https://adk.dev/)
[![agents-cli](https://img.shields.io/badge/agents--cli-Extension%20v1.10%2B-4285F4)](https://github.com/google/agents-cli)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-GenAI%20FinOps-F5A800)](https://adk.dev/observability/metrics/)
[![BigQuery](https://img.shields.io/badge/BigQuery-Native-669DF6)](https://cloud.google.com/bigquery)

> **Disclaimer:** Not an officially supported Google product. Not eligible for the [Google OSS Vulnerability Rewards Program](https://bughunters.google.com/open-source-security). Recipes are for demonstration and as starting points, not production use.

---

## 🌟 Overview & Problem Statement

While Google ADK's built-in OpenTelemetry telemetry (`adk.dev/observability/metrics/`) and `adk eval` efficiency metrics (`token_usage_v1` in `google-adk >= 2.10`, `invocation_duration_v1` in `google-adk >= 2.7.0`) record vendor-neutral token counts (`input`, `output`, `cache_read.input_tokens`, and `reasoning.output_tokens`) as informational telemetry, developers building multi-agent workflows still need shift-left **rate-card dollar math**, **turn-by-turn context debugging**, **prescriptive optimization recommendations**, and **pass/fail CI/CD cost gates** during local development:
1. **Context Cache Misses:** Repeatedly paying full input price for static system instructions, tool schemas, and multi-turn conversation history instead of leveraging **75%–95%**-discounted cached prefix tokens (`ContextCacheConfig`).
2. **Reasoning & Routing Overhead:** Unbounded `thoughts_token_count` (`BuiltInPlanner` / `ThinkingConfig`) billed at output token rates, and frontier models handling simple deterministic sub-agent routing turns.

**ADK Tokenomics Plugin (`adk_tokenomics_plugin`)** is a lightweight `BasePlugin` middleware, visual Token Control Tower (`adk-tokenomics web`), and CLI developer suite for Google ADK that decomposes every agent turn into **4 token buckets** (**Fresh Input**, **Cached Prefix**, **Visible Output**, and **Reasoning/Thinking**), calculates exact USD costs and counterfactual cache/tiering savings, emits supplemental OpenTelemetry USD cost metrics (`gen_ai.client.cost.usd`), and enforces CI/CD budget gates.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        Your Google ADK Agent Fleet                         │
│   LlmAgent • SequentialAgent • LoopAgent • ParallelAgent • Custom Agent    │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │
                   ┌──────────────────┴──────────────────┐
                   ▼                                     ▼
┌─────────────────────────────────────┐ ┌────────────────────────────────────┐
│    ADKTokenomicsPlugin (BasePlugin) │ │  BigQueryAgentAnalyticsPlugin      │
│ • before/after_model_callback       │ │ • <dataset>.adk_agent_events       │
│ • after_tool_callback & on_event    │ │ • <dataset>.v_llm_response         │
│ • Local JSON trace auto-export      │ └─────────────────┬──────────────────┘
│ • OTel gen_ai.client.cost.usd       │                   │
└──────────────────┬──────────────────┘                   │
                   └──────────────────┬───────────────────┘
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│            ADK Tokenomics CLI, Extension & Web Observatory                 │
│  • agents-cli tokenomics web     | adk-tokenomics web     (M3 UI :8080)    │
│  • agents-cli tokenomics profile | adk-tokenomics profile (5 FinOps Rules) │
│  • agents-cli eval budget        | adk-tokenomics assert  (CI/CD Gate)     │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 🏗️ Architecture & How It Works

### 1. Native `BasePlugin` Hook Lifecycle (Works with Workflow Agents)
[`ADKTokenomicsPlugin`](adk_tokenomics_plugin/middleware/adk_callback.py) subclasses `google.adk.plugins.base_plugin.BasePlugin`. Because plugins are registered at the **ADK `App` or `Runner` level**, callbacks automatically propagate down the entire agent execution tree:
- **Supported Agent Types:** Single `LlmAgent`, multi-agent `sub_agents` hierarchies, **Workflow Agents** (`SequentialAgent`, `LoopAgent`, `ParallelAgent`), and custom `BaseAgent` orchestrations.
- **Callback Interception:**
  - `before_model_callback` / `after_model_callback`: Captures per-turn latency, active sub-agent identity (`callback_context.agent_name`), workflow stage (`🎯 Root`, `🧰 AgentTool`, `⚡ Parallel`, `🔁 Loop`, `➡️ Sequential`, and compound stages such as `AgentTool • Parallel • Loop`), model ID, and `usage_metadata` (`prompt_token_count`, `tool_use_prompt_token_count`, `cached_content_token_count`, `candidates_token_count`, `thoughts_token_count`).
  - `after_tool_callback`: Captures invoked tool names, arguments, and on-demand skills (`SkillToolset` / `load_skill`, excluding `unload_skill`) executed during each turn.
  - `on_event_callback` & `close()`: Fallback listener for runner events that emit `usage_metadata` directly on the event stream, plus `await plugin.close()` to flush queued/in-flight BigQuery batches and archive active session traces cleanly.

### 2. 4-Bucket Token Decomposition & Rate-Card Math
For every LLM turn, [`PricingEngine`](adk_tokenomics_plugin/core/pricing.py) normalizes provider prefixes (`publishers/google/models/...`, `vertex_ai/...`, `anthropic/...`) and decomposes token usage into four distinct cost buckets:

| Token Bucket | ADK `usage_metadata` Source | Billing Formula |
| :--- | :--- | :--- |
| **Fresh Input Tokens** ($T_{\text{fresh}}$) | `(prompt_token_count + tool_use_prompt_token_count) - cached_content_token_count` | $(T_{\text{fresh}} / 10^6) \times R_{\text{input}}$ |
| **Cached Prefix Tokens** ($T_{\text{cached}}$) | `cached_content_token_count` | $(T_{\text{cached}} / 10^6) \times R_{\text{cached}}$ |
| **Visible Output Tokens** ($T_{\text{output}}$) | `candidates_token_count` | $(T_{\text{output}} / 10^6) \times R_{\text{output}}$ |
| **Reasoning / Thinking Tokens** ($T_{\text{think}}$) | `thoughts_token_count` | $(T_{\text{think}} / 10^6) \times R_{\text{output}}$ |

- **Actual Turn Cost:** $C_{\text{actual}} = C_{\text{fresh}} + C_{\text{cached}} + C_{\text{output}} + C_{\text{think}}$
- **Hypothetical Uncached Cost:** $C_{\text{uncached}} = \left(\frac{T_{\text{fresh}} + T_{\text{cached}}}{10^6} \times R_{\text{input}}\right) + C_{\text{output}} + C_{\text{think}}$
- **Context Cache Dollar Savings:** $S_{\text{cache}} = \max(0, C_{\text{uncached}} - C_{\text{actual}})$
- **Effective Bill Discount %:** $\frac{S_{\text{cache}}}{C_{\text{uncached}}} \times 100\%$

### 3. OpenTelemetry (`OTel`) FinOps Cost Bridge
Google ADK's built-in OpenTelemetry instrumentation (`adk.dev/observability/metrics/`) records token counts (`gen_ai.client.token.usage` and `adk.experimental.invoke_agent.*`) and operation latency (`gen_ai.client.operation.duration`). To avoid duplicating token histograms while adding rate-card cost telemetry, [`TokenTracker`](adk_tokenomics_plugin/core/tracker.py) detects the active global `opentelemetry.metrics.MeterProvider` and emits the complementary USD cost histogram:

| OTel Instrument | Type & Unit | Attributes | Purpose |
| :--- | :--- | :--- | :--- |
| `gen_ai.client.cost.usd` | `Histogram` (`USD`) | `gen_ai.operation.name="generate_content"`, `gen_ai.cost.type="actual"`, `gen_ai.response.model`, `gen_ai.agent.name` | Exports real-time per-turn USD spend attributed to each sub-agent |
| `gen_ai.client.cost.usd` | `Histogram` (`USD`) | `gen_ai.operation.name="generate_content"`, `gen_ai.cost.type="cache_savings"`, `gen_ai.response.model`, `gen_ai.agent.name` | Exports counterfactual context cache dollar savings per turn |

---

## 🧠 How `adk-tokenomics profile` Generates Recommendations & Model Tiering

When you run `adk-tokenomics profile`, [`TokenProfiler`](adk_tokenomics_plugin/cli_profiler.py) evaluates every turn in the session against **5 configurable heuristics** defined in [`OptimizationRulesConfig`](adk_tokenomics_plugin/core/config.py) and runs counterfactual pricing simulations via [`PricingEngine`](adk_tokenomics_plugin/core/pricing.py):

### 1. 🎯 Sub-Agent Model Tiering (`model_tiering`)
1. **Lightweight Turn Detection:** Flags turns on non-synthesis sub-agents where `thinking_tokens <= lightweight_turn_max_thinking_tokens` (default: **`256` tokens**) and `output_tokens <= lightweight_turn_max_output_tokens` (default: **`600` tokens**).
2. **Dynamic Same-Provider Cloud Model Selection:** Inspects the turn's model provider (`google`, `anthropic`, `openai`, `open_weight`, or custom) and calls `PricingEngine.get_cheapest_model(provider=provider)` against the active rate card (default `include_self_hosted=False`, skipping zero-cost local entries) to pair heavy reasoning models with lower-cost routing/extraction models - for example:
   - **Google Gemini:** `gemini-4-neon` / `gemini-4-argon` / `gemini-4-hydrogen` → `gemini-3.8-flash`
   - **Anthropic Claude:** `claude-opus-4` / `claude-3-7-sonnet` → `claude-3-5-haiku`
   - **OpenAI:** `gpt-5` / `gpt-4o` → `gpt-4o-mini`
   - **Open-Weight (MaaS) & Custom:** `meta/llama-3.3-70b-instruct` / `deepseek-r1` → `meta/llama-3.1-8b-instruct` (or any custom endpoint registered via `models_config.json` or `tower.register_model()`)
3. **Counterfactual Cost Simulation:** Re-computes what each lightweight turn would have cost on the target lite model using its exact `fresh_tok`, `cached_tok`, `output_tok`, and `think_tok`. If `projected_cost < actual_cost`, it outputs the specific turn/sub-agent labels (e.g., `Turn #1 [router_agent]`) and exact projected `%` and `$` savings.

### 2. ⚡ Prefix Context Caching (`context_caching`)
- **Trigger:** Any non-warmup turn where `cached_tokens == 0` and `fresh_tok > min_cache_tokens` (default: **`1,024` tokens**). When `ADKTokenomicsPlugin` detects `ContextCacheConfig` is already attached to `App` and warm turns hit cache, redundant setup hints are suppressed automatically (and `0`-hit runs emit a prefix/region diagnostic instead).
- **Action & ROI:** Multiplies uncached tokens by $(R_{\text{input}} - R_{\text{cached}})$ (typically **75%–95%** cache discount) to calculate exact missed dollar savings and recommends adding `ContextCacheConfig(min_tokens=1024, ttl_seconds=300)` to the ADK `App`.

### 3. 📦 Multi-Turn History Compaction (`history_compaction`)
- **Trigger:** Max distinct user invocations/turns within any single `session_id` $\ge$ `compaction_turn_threshold` (default: **`4` turns**, with growing prompt context) OR new-user-invocation `fresh_tok` growth per `(session_id, sub_agent)` exceeds `compaction_growth_delta` (default: **`+350` tokens**), aware of `EventsCompactionConfig` when attached to `App`.
- **Action:** Prices per-turn fresh tokens at each turn's actual model rate and recommends configuring `EventsCompactionConfig(compaction_interval=4, overlap_size=1)` on the ADK `App` to summarize older conversation events.

### 4. 🧩 Modular Skills Progressive Disclosure (`modular_skills`)
- **Trigger:** Single-turn `fresh_tok > modular_skills_token_threshold` (default: **`5,000` tokens**) when `SkillToolset` (`load_skill`) is not already active in the session.
- **Action & ROI:** Estimates per-turn dollar savings across each turn's actual model rate assuming `modular_skills_reduction_ratio` (**`60%`**) of static system instructions are moved into on-demand ADK `SkillToolset` (`SKILL.md`) modules loaded via `load_skill`.

### 5. 🧠 Planner & Thinking Budget Tuning (`thinking_budget`)
- **Trigger:** Session-wide or single-turn `thinking_tokens > recommended_thinking_budget` (default: **`256` tokens** noise floor) AND (`output_tokens == 0` OR ratio `thinking_tokens / output_tokens > max_thinking_to_output_ratio`, default: **`0.80x`**).
- **Action:** Pinpoints the top heavy-thinking sub-agents/turns and recommends bounding reasoning via `BuiltInPlanner(thinking_config=ThinkingConfig(thinking_budget=256))` (or `thinking_budget=0` for deterministic routing agents).

### Summary: Mapping the 5 FinOps Rules to Google ADK Configurations

| FinOps Pillar | ❌ Unoptimized ADK Anti-Pattern | ✅ Optimized Google ADK Configuration |
| :--- | :--- | :--- |
| **`model_tiering`** | Frontier model (`gemini-4-argon` / `gemini-4-neon`) on every sub-agent | Route routers & lightweight fetchers to `gemini-3.8-flash` / `gemini-4-hydrogen`; reserve Argon / Neon for synthesis |
| **`context_caching`** | `App(...)` omits `context_cache_config`, re-billing static prompts every turn | `App(..., context_cache_config=ContextCacheConfig(min_tokens=1024, ttl_seconds=300))` |
| **`history_compaction`** | `App(...)` omits `events_compaction_config`, accumulating unbounded chat history | `App(..., events_compaction_config=EventsCompactionConfig(compaction_interval=4, overlap_size=1))` |
| **`modular_skills`** | 5,000+ tokens of static reference playbooks pasted into `LlmAgent(instruction=...)` | Slim `instruction` + `tools=[SkillToolset(skills=...)]` (`SKILL.md` loaded on demand via `load_skill`) |
| **`thinking_budget`** | Unbounded `BuiltInPlanner(ThinkingConfig(include_thoughts=True))` across sub-agents | `ThinkingConfig(thinking_budget=256)` on complex planners and `thinking_budget=0` on deterministic routers |

---

## 🚀 Quickstart & Installation

Choose **Option 1** if you build and evaluate agents with Google [`agents-cli`](https://github.com/google/agents-cli), or **Option 2** for standalone Python / `adk web` projects.

### Option 1: End-to-End with Google `agents-cli` (`v1.10+`)

Installing `adk-tokenomics` as an `agents-cli` extension contributes `agents-cli tokenomics <web|profile|assert>`, `agents-cli eval budget`, and the coding-agent FinOps skill (`.agents/skills/adk-tokenomics/SKILL.md`). Follow the steps below to go from a fresh project to live turn-level telemetry, profiling, and CI/CD budget gates:

#### Step 1 — Prerequisites & Install the Extension

```bash
# 1. Install Google agents-cli (v1.10+; requires uv on PATH)
uv tool install google-agents-cli

# 2. Create a new ADK agent project (or cd into an existing agents-cli project)
agents-cli create my-agent -y
cd my-agent

# 3. Add the adk-tokenomics extension at project scope (recommended)
agents-cli extension add google/adk-recipes --ref adk-tokenomics

# 4. Verify active extension scope, commands (eval.budget, tokenomics), and version range
agents-cli extension list
```

> **Install scope, `v1.10+` floor & local checkouts:** Requires `agents-cli >= 1.10` (`<1.10` fails manifest validation because `skills:` was introduced in `v1.10`). Pass `--global` to `agents-cli extension add` to install at user scope across all projects instead of committing the extension to a single repo. To install from a local clone on disk, prefix the path with `local@` (`agents-cli extension add local@/path/to/adk-tokenomics-plugin` — a bare path is parsed as a GitHub `org/repo`).

#### Step 2 — Add the Runtime Python SDK to the Agent Environment

`agents-cli extension add` vendors the extension CLI under `extensions/adk-tokenomics/` so `agents-cli tokenomics` and `agents-cli eval budget` work immediately via `uv run --no-sync`. To record live token and USD traces inside your ADK agent, add `adk-tokenomics-plugin` to the project's Python environment so `app/agent.py` can import `ADKTokenomicsPlugin`:

```bash
# Project-scope install (local dev & CI evals — zero network/PyPI access, exact version lockstep):
uv add --no-workspace ./extensions/adk-tokenomics

# Optional: include BigQuery streaming sink dependencies
uv add --no-workspace ./extensions/adk-tokenomics --extra bigquery
```

> **`--global` installs & Cloud Deployments (`agents-cli deploy`):**
> - Installing with `--global` vendors the extension under `~/.config/agents-cli/extensions/adk-tokenomics` (`%APPDATA%\agents-cli\extensions\adk-tokenomics` on Windows) rather than `./extensions/adk-tokenomics`.
> - `agents-cli deploy` excludes `/extensions/` from cloud uploads (via `.gcloudignore`).
> - If you installed the extension with `--global`, or if you explicitly want runtime tokenomics telemetry inside a deployed Cloud Run / Agent Engine container, install the SDK from GitHub instead:
>   ```bash
>   uv add "adk-tokenomics-plugin @ git+https://github.com/google/adk-recipes.git@adk-tokenomics"
>   # Or with BigQuery sink extras:
>   uv add "adk-tokenomics-plugin[bigquery] @ git+https://github.com/google/adk-recipes.git@adk-tokenomics"
>   ```

#### Step 3 — Instrument `app/agent.py` (Opt-In via `.env`)

Attach `ADKTokenomicsPlugin` to your `App` in `app/agent.py` behind an opt-in `TOKENOMICS_ENABLED=1` check so production deployments never require the package unless explicitly enabled:

```python
import os

from dotenv import load_dotenv
from google.adk.apps import App

load_dotenv()  # agents-cli imports app/agent.py before reading .env

plugins = []
if os.getenv("TOKENOMICS_ENABLED") == "1":
    from adk_tokenomics_plugin import ADKTokenomicsPlugin

    plugins.append(
        ADKTokenomicsPlugin(
            app_name="app",
            auto_export_path="artifacts/traces/live_adk_session.json",
        )
    )

app = App(
    name="app",
    root_agent=root_agent,
    plugins=plugins,
)
```

Enable telemetry locally in your git-ignored `.env` file (or export `TOKENOMICS_ENABLED=1` in your shell / CI job):

```bash
echo "TOKENOMICS_ENABLED=1" >> .env
```

- **Why `load_dotenv()` first:** `agents-cli` imports `app/agent.py` before loading `.env`. `python-dotenv` already ships with `google-adk`, so no extra dependency is needed, and `.env` is excluded from git and `agents-cli deploy`.
- **`app_name` label:** Recorded on every turn and BigQuery event row; matching `App(name=...)` keeps telemetry labels aligned, though they do not strictly have to match.
- **Trace privacy & `.gitignore`:** Local trace JSON files (`artifacts/traces/*.json`, written with mode `0600`) record user prompts and truncated agent responses by default. Add `artifacts/` to your project `.gitignore` (`echo "artifacts/" >> .gitignore`) so conversation content is never committed to git, or set `TOKENOMICS_REDACT_CONTENT=1` (or `ADKTokenomicsPlugin(..., redact_content=True)`) to redact prompt/response text while preserving full token, tool, and USD cost telemetry.
- **Live working file vs. archived sessions:** `auto_export_path` (`artifacts/traces/live_adk_session.json`) holds the active in-progress session. When a new session starts or the process exits, finished sessions archive automatically as `artifacts/traces/<session_id>.json` (and `await plugin.close()` flushes queued BigQuery batches). Always point `profile`, `eval budget`, and `web` at the `artifacts/traces/` directory (or `.`), not the transient `live_adk_session.json` file. Relative `auto_export_path` values resolve from the agent process's working directory (the project root for `agents-cli playground`, `agents-cli eval`, and `adk web`); use an absolute path if launching from elsewhere.

#### Step 4 — Generate Traces (`playground` or `eval`)

Run your instrumented agent interactively or over your evaluation dataset to populate `artifacts/traces/<session_id>.json`:

```bash
# Workflow A — Interactive local playground + live Web Observatory streaming
# Terminal 1: launch the Material 3 Dark Slate observatory (streams turns live as they complete)
agents-cli tokenomics web --traces artifacts/traces/ --port 8080
# Terminal 2: chat interactively with your agent (or run single-shot `agents-cli run "<prompt>"`)
agents-cli playground

# Workflow B — Automated evaluation inference (or `agents-cli eval run` to generate + grade quality)
agents-cli eval generate
```

> **How `agents-cli eval generate` coexists with plugin traces:** `agents-cli eval generate` (and `eval run`) writes an evaluation transcript `artifacts/traces/traces_<timestamp>.json` alongside the plugin's `<session_id>.json` files. Because `traces_<timestamp>.json` contains no LLM token usage metadata, `eval budget` and `profile` automatically evaluate the instrumented `<session_id>.json` traces and report `traces_<timestamp>.json` as skipped (`skipped_artifacts` in JSON / `[INFO]` in text). If an agent ran *without* `ADKTokenomicsPlugin`, `eval budget` fails closed (`exit 1`) and running `profile` directly on `traces_<timestamp>.json` labels character-count approximations with an `⚠️ ESTIMATED` banner (`≈$` in the web UI).

#### Step 5 — Profile Turn Waterfalls & Enforce CI/CD Budget Gates

```bash
# 1. Inspect the turn-by-turn context waterfall, sub-agent cost share & 5 FinOps rules
# (Given a directory, profile analyzes the newest session; pass -t <file> for a specific session)
agents-cli tokenomics profile -t artifacts/traces/
agents-cli tokenomics profile -t artifacts/traces/ --format json

# 2. Enforce pass/fail unit-economics gates across all sessions (exits 0 on pass, 1 on breach)
# `--traces .` discovers both `artifacts/traces/` and `.adk/eval_history/`
agents-cli eval budget \
  --traces . \
  --max-cost-per-turn 0.005 \
  --max-tokens-per-turn 5000 \
  --max-latency-ms 15000

# Equivalent via the top-level tokenomics command (add --format json for CI pipelines):
agents-cli tokenomics assert --traces artifacts/traces/ --max-cost-per-turn 0.005 --format json
```

> **Cache-rate gate tip:** Add `--min-cache-rate 0.60` only once your static prompt prefix exceeds ~1,024 tokens (where provider context caching activates); shorter prompts report a `0%` cache hit rate by design.

#### Step 6 — Coding-Agent Skill & Extension Lifecycle (Sync, Update & Safe Uninstall)

- **Autonomous Optimization via `.agents/skills/adk-tokenomics/SKILL.md`:** Coding agents (Gemini CLI, Claude Code, Antigravity, Cursor) automatically load the installed skill to instrument `app/agent.py`, run `agents-cli tokenomics profile --format json`, apply the 5 ADK FinOps fixes (`ContextCacheConfig`, `EventsCompactionConfig`, `SkillToolset`, `ThinkingConfig`, and sub-agent model tiering), and verify cost reductions with `agents-cli eval budget`.
- **Team Clone Restore, Pin Updates & Safe Uninstall:**
  ```bash
  # Restore pinned extensions & skills after cloning a repo with committed agents-cli-extensions.yaml
  agents-cli install

  # Advance the extension SHA pin and refresh .agents/skills/adk-tokenomics/
  agents-cli extension update adk-tokenomics

  # Safe uninstall order (when SDK was added from ./extensions/adk-tokenomics):
  # Remove the Python dependency FIRST so pyproject.toml/uv.lock never point at a deleted folder
  uv remove adk-tokenomics-plugin
  agents-cli extension remove adk-tokenomics
  ```
- **Operational Notes:**
  - **`-h` vs. `--help`:** `agents-cli` answers `--help` itself with extension metadata, so pass `-h` to inspect tool flags (`agents-cli tokenomics profile -h`, `agents-cli eval budget -h`).
  - **Shell-relative paths & `uv`:** Extension commands run via `uv run --no-sync` (`uv` must be on `PATH`, as it is for `agents-cli`), and relative path flags (`-t`/`--traces`, `--prompts-file`) resolve from the shell directory where you run the command.
  - **Vendored folder & dual-scope updates:** Do not edit files inside `extensions/adk-tokenomics/` (`agents-cli extension update` replaces that directory). Avoid installing the extension in both project and `--global` scopes from different sources: `agents-cli extension update` runs across every scope, so `update --to <sha>` fails for any scope whose source lacks that commit (while still updating the other scope).

---

### Option 2: Standalone Python Package (`adk web` or Custom Runners)

```bash
# Zero-cloud-dependency install (local web observatory, CLI profiler, offline demo, and CI/CD assert)
pip install -e .

# Include Google ADK & GenAI SDKs to run live agents (e.g. `adk web examples`)
pip install -e ".[adk]"

# Optional: include BigQuery sink dependencies for fleet telemetry
pip install -e ".[bigquery]"

# Verify CLI installation
adk-tokenomics --help
```

#### Attaching Directly to Any Google ADK Agent (`App` or `InMemoryRunner`)

```python
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from adk_tokenomics_plugin import ADKTokenomicsPlugin, TokenControlTower

# Option A: Direct 2-line ADK App plugin registration with automatic JSON trace export
app = App(
    name="travel_planner",
    root_agent=root_agent,
    plugins=[
        ADKTokenomicsPlugin(
            app_name="travel_planner",
            auto_export_path="artifacts/traces/live_adk_session.json",
        )
    ],
)

# Option B: Via TokenControlTower (supports memory or BigQuery sinks)
tower = TokenControlTower(
    mode="dev",  # "dev" (in-memory + local JSON) or "prod" (BigQuery)
    sink="memory",  # "memory" or "bigquery"
    project_id="my-gcp-project",
    dataset_id="bq_adk_ds",
)
tokenomics_plugin = tower.create_adk_plugin(
    app_name="travel_planner",
    auto_export_path="artifacts/traces/live_adk_session.json",
)
# Attach plugins via App (InMemoryRunner(plugins=...) is deprecated upstream in favor of app=)
runner = InMemoryRunner(
    app=App(
        name="travel_planner",
        root_agent=root_agent,
        plugins=[tokenomics_plugin],
    ),
)
```

> **Trace files:** while a session is running, the plugin writes it to `auto_export_path`. When a new session starts or the process exits, the finished session is saved as `artifacts/traces/<session_id>.json`, so point `profile`, `assert`, and `web` at the `artifacts/traces/` directory.

---

## 🛠️ CLI Command Reference

Every CLI subcommand works identically whether invoked via the **`agents-cli` extension** (`adk-tokenomics` does not need to be on `PATH`) or via a **standalone Python install**:

| Capability | `agents-cli` Extension Command | Standalone CLI Command | Flag Help |
| :--- | :--- | :--- | :--- |
| **Visual Web Observatory** | `agents-cli tokenomics web ...` | `adk-tokenomics web ...` | `agents-cli tokenomics web -h` |
| **Context Waterfall & FinOps Advisor** | `agents-cli tokenomics profile ...` | `adk-tokenomics profile ...` | `agents-cli tokenomics profile -h` |
| **CI/CD Financial & Token Budget Gate** | `agents-cli eval budget ...` *(or `agents-cli tokenomics assert ...`)* | `adk-tokenomics assert ...` | `agents-cli eval budget -h` |

> **What `--traces` (`-t`) accepts across commands:** Pass a single trace file (`artifacts/traces/<session_id>.json`), a directory (`artifacts/traces/`), `.` (the project root, which searches `artifacts/traces/` and `.adk/eval_history/`), or omit `--traces` to default to `artifacts/traces/`. Under `agents-cli`, CLI remediation hints (`-> Run: agents-cli tokenomics profile -t <file>`) and BigQuery dependency hints (`uv add --no-workspace ./extensions/adk-tokenomics --extra bigquery && agents-cli install`) automatically match the extension invocation mode.

### 1. `adk-tokenomics web` (`agents-cli tokenomics web`) — Interactive Visual Observatory
Launches the embedded **Google Material 3 Dark Slate** web dashboard on `http://127.0.0.1:8080` (matching the look and feel of `adk web`).

```bash
# Launch with automatic local session + BigQuery detection
adk-tokenomics web --port 8080

# Inspect local trace file or directory offline
adk-tokenomics web --traces artifacts/traces/

# Connect to a specific BigQuery dataset & analytics view
adk-tokenomics web --project my-gcp-project --dataset bq_adk_ds --view v_llm_response

# Bind 0.0.0.0 behind Docker port-mapping or Cloud Run HTTPS with explicit Host/Origin allowlist
adk-tokenomics web --host 0.0.0.0 --port 8080 --allowed-host "*.run.app" --allowed-host "localhost:9090" --no-browser
```

**Dashboard Highlights:**
- **4 Real-Time KPI Cards:**
  1. **Accumulated Session Spend:** Total cost + average cost per turn (`Avg $0.00xxx / turn`).
  2. **Context Cache Efficiency:** Prefix cache hit ratio (`%`) + counterfactual dollar savings (`Saved $0.00xxx (Y.Y% off total bill)`).
  3. **Input Context Tokens:** Total prompt tokens split into `Fresh` vs. `Cached`.
  4. **Reasoning & Output Tokens:** Total completion tokens split into `Visible` vs. `Thoughts (Z%)`.
- **Sortable Analytics Tables:** Click any column header in **Agent Breakdown**, **Session Turn-by-Turn History**, or **Tool Usage & Execution Telemetry** to sort ascending/descending.
- **Interactive Rate Card Console:** Edit 1P/3P/Open-Weight token rates (`models_config.json`) in real time to simulate cost and caching scenarios.
- **Container & Reverse-Proxy Host Protection:** Loopback hosts (`127.0.0.1`, `localhost`, `[::1]`) are trusted by default; when binding `--host 0.0.0.0` behind Docker port mapping or Cloud Run HTTPS, allow external `Host`/`Origin` headers via repeatable `--allowed-host <host|*.domain|host:port>` flags or `TOKENOMICS_ALLOWED_HOSTS` (bare `*` is rejected fail-closed).

---

### 2. `adk-tokenomics profile` (`agents-cli tokenomics profile`) — Turn-by-Turn Context Waterfall & FinOps Advisor
Renders an ANSI turn-by-turn token waterfall in the terminal and runs the 5 optimization rules.

```bash
# Profile the newest session in a trace directory (or pass a specific <session_id>.json file)
adk-tokenomics profile -t artifacts/traces/

# Emit machine-readable JSON output (sub-agent breakdown, turns, and recommendations)
adk-tokenomics profile -t artifacts/traces/ --format json

# Profile a live BigQuery session ID (correlates ADK USER_MESSAGE_RECEIVED, LLM_REQUEST, TOOL_COMPLETED & LLM_RESPONSE events)
adk-tokenomics profile -s session_abc123 --project my-gcp-project --dataset bq_adk_ds

# Customize heuristic thresholds on the CLI
adk-tokenomics profile \
  -t artifacts/traces/ \
  --min-cache-tokens 2048 \
  --compaction-turns 6 \
  --prompts-file custom_prompts.json
```

> **`--prompts-file` / `prompts_config_path` Schema, Validation & Precedence:** `--prompts-file` (and `TokenomicsConfig(prompts_config_path=...)`) validates file existence and JSON object (`dict`) schema **fail-fast** (raising `ValueError` in Python or exiting `1` on the CLI if the file is missing, malformed JSON, non-object JSON, or contains unknown `"rules"` keys). When `"rules"` defaults are defined inside `--prompts-file`, explicit CLI threshold flags (`--min-cache-tokens`, `--compaction-turns`) always take precedence over file values.

Example `custom_prompts.json` (both `"rules"` and `"prompts"` sections are optional; a flat JSON object of `{rule_key: template}` is also accepted):

```json
{
  "rules": {
    "min_cache_tokens": 2048,
    "cache_ttl_seconds": 600,
    "compaction_turn_threshold": 6,
    "modular_skills_token_threshold": 8000,
    "max_thinking_to_output_ratio": 0.60,
    "recommended_thinking_budget": 512,
    "lightweight_turn_max_output_tokens": 450,
    "recommended_lite_model": "publishers/google/models/gemini-3.8-flash"
  },
  "prompts": {
    "context_caching": "⚡ [Cache Policy]: Uncached prompt in {turns_str}. Est. savings: ${estimated_cache_savings:.4f} ({cache_discount_pct:.0f}% off)."
  }
}
```

---

### 3. `adk-tokenomics assert` (`agents-cli eval budget`) — Shift-Left CI/CD Financial & Token Budget Gate
While `adk eval` captures `token_usage_v1` (`google-adk >= 2.10`) and `invocation_duration_v1` (`google-adk >= 2.7.0`) strictly as informational metrics without pass/fail thresholds or dollar pricing, `adk-tokenomics assert` enforces **pass/fail unit economics gates** in CI/CD pipelines. It accepts plugin session traces or native `adk eval` EvalSetResult files (automatically discovering `<dir>/.adk/eval_history/*.evalset_result.json` while skipping non-result `*.evalset.json` / `test_config.json` configs, and deduplicating turns when `live_adk_session.json`/`ci_eval.json` coexists with `<session_id>.json`) and **fails closed**: it exits with code `1` when artifacts are missing, empty (`0` turns), uninstrumented (`eval_cases` without token usage metadata), or unparseable (opt out for missing or `0`-turn trace artifacts with `--allow-empty`), when `--max-latency-ms` is set but no artifact reports latency, or when any budget threshold is breached. On failure, text output prints `-> Run: adk-tokenomics profile -t <file>` remediation hints and `--format json` includes `"failed_artifacts"`.

```bash
adk-tokenomics assert \
  --traces artifacts/traces/ \
  --max-cost-per-turn 0.005 \
  --max-session-cost 0.05 \
  --min-cache-rate 0.60 \
  --max-tokens-per-turn 5000 \
  --max-session-tokens 50000 \
  --max-latency-ms 15000 \
  --strict-pricing \
  --format json
```

> **Cache-rate & zero-flag gate behavior:** `assert` / `eval budget` requires at least one gate flag (failing closed if invoked with zero thresholds on non-empty traces). Context caching only kicks in once prompts reach about 1,024 tokens; agents with short prompts report a 0% cache-hit rate and will fail `--min-cache-rate`, so leave that flag out until your static prefix crosses that size.

| CLI Gate Flag | Evaluated Formula Across $N$ Turns | Fails (`exit 1`) When |
| :--- | :--- | :--- |
| `--max-cost-per-turn <float>` | $\frac{1}{N}\sum \text{total\_cost}$ | Average USD cost per turn exceeds threshold |
| `--max-session-cost <float>` | $\max_{s} \sum_{t \in s} \text{total\_cost}_t$ | Cumulative USD cost of any session exceeds cap |
| `--min-cache-rate <0.0–1.0>` | $\frac{\sum \text{cached\_tokens}}{\sum (\text{input\_tokens} + \text{cached\_tokens})}$ | Context cache hit ratio drops below minimum |
| `--max-tokens-per-turn <int>` | $\frac{1}{N}\sum (\text{in} + \text{cached} + \text{out} + \text{think})$ | Average total tokens per turn exceeds cap |
| `--max-session-tokens <int>` | $\max_{s} \sum_{t \in s} \text{total\_tokens}_t$ | Cumulative total tokens of any session exceeds cap |
| `--max-latency-ms <int>` | $\frac{1}{N_{\text{lat}}}\sum \text{latency\_ms}$ over turns that report latency | Average latency (ms) exceeds budget, or no artifact reports latency (fail-closed) |
| `--strict-pricing` | `any(turn.pricing_estimated)` | Any turn uses heuristic fallback pricing for an unregistered model ID |

> **Phase 2 Roadmap (Post-MVP):** Multi-model Pareto evaluation (`arena`), fleet-wide BigQuery FinOps executive aggregation (`finops`), and managed Cloud Run deployment (`deploy cloud_run`) are planned for Phase 2 once multi-project production BigQuery telemetry sinks are standardized.

---

## ⚙️ Advanced Configuration & Extensibility

### Custom Model Registration (1P, 3P, or Self-Hosted Open-Weight)
[`PricingEngine`](adk_tokenomics_plugin/core/pricing.py) ships with built-in rate cards in [`models_config.json`](adk_tokenomics_plugin/models_config.json) for **Google Gemini** (`gemini-3.8-flash`, `gemini-4-hydrogen`, `gemini-4-argon`, `gemini-4-neon`), **Anthropic Claude** (`claude-3-5-haiku`, `claude-3-7-sonnet`, `claude-sonnet-5`, `claude-opus-4`), **OpenAI** (`gpt-4o-mini`, `gpt-4o`, `gpt-5`), and **Open-Weight** models (`llama-3.1-8b`, `llama-3.3-70b`, `llama-3.1-405b`, `deepseek-v3`, `deepseek-r1`, `mistral-small`, `mistral-large`, `qwen-2.5-72b`, `gemma-3-27b`, `ollama/local-open-weight`). Unregistered model IDs fall back to family/default rate cards and are explicitly flagged with `pricing_estimated: true` (`≈$` in the profiler and Web UI) plus a one-time warning.

> **Dynamic Per-Turn Pricing & Rate-Card Customization:** Every LLM turn is priced dynamically using the exact model returned by that sub-agent's execution (`llm_response.model_version` / `llm_request.model`), with `TOKENOMICS_DEFAULT_MODEL` used only as a fallback when an event carries no model metadata. Packaged entries in `models_config.json` provide illustrative baseline rate cards for local simulation - override them anytime via `models_config.json`, the Web UI `Rate Card ⚙️` editor, or `tower.register_model()` to reflect current provider pricing or enterprise discounts.

You can register new model releases or custom rate cards at runtime:

```python
tower.register_model(
    model_id="anthropic/claude-sonnet-4-5",
    input_per_million=3.00,
    cached_per_million=0.30,
    output_per_million=15.00,
    provider="anthropic",
    name="Claude Sonnet 4.5",
)
```

> **Rate-Card File Resolution Precedence:** Rate cards are resolved in order: (1) explicit `TokenomicsConfig(models_config_path=...)`, (2) `$TOKENOMICS_MODELS_CONFIG_PATH` / `$TOKENOMICS_MODELS_CONFIG`, (3) project-local `./models_config.json`, (4) user XDG config `$XDG_CONFIG_HOME/adk-tokenomics/models_config.json` (`~/.config/adk-tokenomics/models_config.json`, where Web UI edits persist without mutating the packaged file; supports `"deleted_ids"` and `"replace_defaults": true`), and (5) packaged `adk_tokenomics_plugin/models_config.json`.

### Custom Optimization Rules, Prompt Templates & Callbacks
All 5 heuristic thresholds are user-configurable via [`OptimizationRulesConfig`](adk_tokenomics_plugin/core/config.py), `--prompts-file` JSON, or runtime methods on [`TokenControlTower`](adk_tokenomics_plugin/tower.py) (note: the Web UI `Rate Card ⚙️` modal edits `models_config.json` pricing only; optimization rules are configured via Python, JSON, or CLI):

| `OptimizationRulesConfig` Field | Type | Default | Rule Controlled & Purpose |
| :--- | :--- | :--- | :--- |
| `min_cache_tokens` | `int` | `1024` | **`context_caching`**: Minimum uncached `fresh_tok` on a turn (`cached_tokens == 0`) to trigger caching hint |
| `cache_ttl_seconds` | `int` | `300` | **`context_caching`**: Recommended `ContextCacheConfig(ttl_seconds=...)` value rendered in hint |
| `compaction_turn_threshold` | `int` | `4` | **`history_compaction`**: Distinct user turns within a `session_id` that triggers event compaction hint |
| `compaction_growth_delta` | `int` | `350` | **`history_compaction`**: Per-turn `fresh_tok` growth per `(session_id, sub_agent)` that triggers compaction |
| `compaction_interval` | `int` | `4` | **`history_compaction`**: Recommended `EventsCompactionConfig(compaction_interval=...)` rendered in hint |
| `compaction_overlap` | `int` | `1` | **`history_compaction`**: Recommended `EventsCompactionConfig(overlap_size=...)` rendered in hint |
| `modular_skills_token_threshold` | `int` | `5000` | **`modular_skills`**: Single-turn `fresh_tok` threshold (without active `SkillToolset`) to recommend `SKILL.md` progressive disclosure |
| `modular_skills_reduction_ratio` | `float` | `0.60` | **`modular_skills`**: Assumed static-prompt fraction (`0.0–1.0`) saved when estimating `SKILL.md` ROI |
| `max_thinking_to_output_ratio` | `float` | `0.80` | **`thinking_budget`**: Maximum `thinking_tokens / output_tokens` ratio before recommending `ThinkingConfig` cap |
| `recommended_thinking_budget` | `int` | `256` | **`thinking_budget`**: Noise floor (`> 256` tokens) and target `ThinkingConfig(thinking_budget=...)` value |
| `enable_model_tiering_hint` | `bool` | `True` | **`model_tiering`**: Enables/disables counterfactual lite-model tiering simulation |
| `lightweight_turn_max_output_tokens` | `int` | `600` | **`model_tiering`**: Max `output_tokens` classified as a lightweight turn |
| `lightweight_turn_max_thinking_tokens` | `int` | `256` | **`model_tiering`**: Max `thinking_tokens` classified as a lightweight turn |
| `recommended_lite_model` | `str \| None` | `None` | **`model_tiering`**: Explicit lite model ID override (when `None`, auto-selects cheapest same-provider cloud model) |

```python
from adk_tokenomics_plugin import TokenControlTower, OptimizationRulesConfig

tower = TokenControlTower(
    optimization_rules=OptimizationRulesConfig(
        min_cache_tokens=2048,
        compaction_turn_threshold=5,
        max_thinking_to_output_ratio=0.60,
        lightweight_turn_max_output_tokens=450,
        recommended_lite_model="gemini-3.8-flash",
    )
)

# Update rule thresholds or override any template ('context_caching', 'history_compaction',
# 'modular_skills', 'thinking_budget', 'model_tiering', 'well_optimized') at runtime:
tower.configure_optimization_rules(recommended_thinking_budget=512)
tower.set_optimization_prompt(
    "thinking_budget",
    "🧠 [Org Thinking Cap]: {total_thinking:,} thought tokens vs. {total_output:,} output tokens. Cap at {recommended_thinking_budget}.",
)


# Register a custom FinOps rule callback (ctx keys: total_turns, total_fresh, total_cached,
# total_output, total_thinking, total_cost, thinking_ratio, uncached_prompts, turns)
def flag_high_cost_session(ctx: dict) -> str | None:
    if ctx["total_cost"] > 0.05:
        return f"🚨 [Budget Alert]: Session cost (${ctx['total_cost']:.4f}) exceeded $0.05 SLA."
    return None


tower.add_recommendation_rule(flag_high_cost_session)

# Optional: build a {system_instruction, user_prompt} payload for LLM-assisted FinOps review
advisor_payload = tower.get_finops_advisor_prompt(title="Checkout Agent Trace")
```

### Python Unit Test & Pytest Budget Assertions
Alongside the CLI `adk-tokenomics assert` gate, [`adk_tokenomics_plugin.testing`](adk_tokenomics_plugin/testing/) provides Pytest assertion functions and [`TokenomicsTestCase`](adk_tokenomics_plugin/testing/test_case.py) for isolated in-process regression tests (note: `assert_cache_ratio_above` and `assertCacheHitRatioGreaterThan` take a percentage in `0.0–100.0`, whereas the CLI `--min-cache-rate` flag takes a ratio in `0.0–1.0`):

```python
from adk_tokenomics_plugin import (
    TokenomicsTestCase,
    assert_cost_under,
    assert_cache_ratio_above,
    assert_token_budget,
    assert_thinking_budget,
)


# Option A: Standalone Pytest assertions (accept a TokenTracker or summary dict)
def test_agent_unit_economics(tracker):
    assert_cost_under(tracker, max_dollars=0.01)
    assert_cache_ratio_above(tracker, min_ratio_pct=60.0)
    assert_token_budget(tracker, max_tokens=8000)
    assert_thinking_budget(tracker, min_tokens=0, max_tokens=512)


# Option B: unittest.TestCase subclass with isolated per-block tracker
class TestTravelAgentFinOps(TokenomicsTestCase):
    def test_booking_turn_budget(self):
        with self.track_tokens() as tracker:
            # Attach ADKTokenomicsPlugin(tracker=tracker) or call tracker.record_turn(...)
            tracker.record_turn(
                session_id="test_s1",
                app_name="router",
                model_name="gemini-3.8-flash",
                user_query="Find flights from SFO to NRT",
                agent_response="Found 2 nonstop flight options.",
                input_tokens=400,
                cached_tokens=1600,
                output_tokens=120,
            )
            self.assertCostLessThan(tracker, max_dollars=0.005)
            self.assertCacheHitRatioGreaterThan(tracker, min_ratio_pct=75.0)
            self.assertTokenBudget(tracker, max_tokens=3000)
            self.assertThinkingTokensWithin(
                tracker, min_tokens=0, max_tokens=256
            )
```

### Decorating Standalone Functions & Generators (`@track_tokens`)
For custom Python pipelines, RAG helpers, or non-ADK functions (sync, async, sync-generator, or async-generator) that return a `google-genai` / OpenAI / Anthropic response object (or a `(text, usage_dict)` tuple), use [`@track_tokens`](adk_tokenomics_plugin/middleware/decorator.py):

```python
from adk_tokenomics_plugin import track_tokens


@track_tokens(
    app_name="rag_reranker",
    model_name="publishers/google/models/gemini-3.8-flash",
    tracker=tower.tracker,  # Optional; uses a shared in-memory tracker when omitted
)
async def summarize_chunks(prompt: str):
    return await genai_client.aio.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
    )
```

### Storage Sinks & Environment Variables

| Sink Mode (`sink=`) | Backing Store | Best For |
| :--- | :--- | :--- |
| `memory` *(default in `dev`)* | `InMemorySink` + optional JSON trace export (`auto_export_path`) | Local `adk web` development, CLI profiling & CI/CD assertions |
| `bigquery` *(default in `prod`)* | `BigQuerySink` (`adk_agent_events` + `v_llm_response` / `BigQueryAgentAnalyticsPlugin`) | Production fleet telemetry & Looker/BigQuery analytics |

> **BigQuery Schema & Legacy v1.0.1 Flat-Table Compatibility:** `provision_bigquery_table()` and `BigQuerySink` default to the unified ADK event schema (`adk_agent_events` + `v_llm_response` via `CREATE VIEW IF NOT EXISTS`). Existing legacy v1.0.1 flat BigQuery tables stay **writable** automatically via `BigQuerySink` schema auto-detection (or `provision_bigquery_table(..., legacy_flat_schema=True)`), whereas ADK event tables (`adk_agent_events` / `agent_events` and `v_llm_response`) are **required** for `adk-tokenomics web` and `adk-tokenomics profile -s` BigQuery analytics queries (as well as the Phase 2 `finops` command).

| Configuration Target | Environment Variables Checked (in priority order) | Default Fallback |
| :--- | :--- | :--- |
| **GCP Project ID** | `GOOGLE_CLOUD_PROJECT`, `GCP_PROJECT`, `BIGQUERY_PROJECT_ID` | Auto-detected via `google.auth.default()` |
| **BigQuery Dataset** | `BQ_ANALYTICS_DATASET_ID`, `BIGQUERY_DATASET_ID`, `BIGQUERY_DATASET`, `BQ_DATASET_ID` | `bq_adk_ds` |
| **Base Events Table** | `BQ_ANALYTICS_TABLE_ID`, `BIGQUERY_TABLE_ID`, `BIGQUERY_TABLE`, `BQ_TABLE_ID` | `adk_agent_events` |
| **Analytics View** | `BQ_ANALYTICS_VIEW_ID`, `BIGQUERY_VIEW_ID`, `BIGQUERY_VIEW`, `BQ_VIEW_ID` | `v_llm_response` |
| **SDK Mode & Sink** | `TOKENOMICS_MODE` (`dev` \| `prod`), `TOKENOMICS_SINK` (`memory` \| `bigquery`) | `dev` / `memory` (`bigquery` in `prod`) |
| **Default Baseline Model** | `TOKENOMICS_DEFAULT_MODEL` | `publishers/google/models/gemini-3.8-flash` |
| **App Name, Export & Redaction** | `TOKENOMICS_APP_NAME`, `TOKENOMICS_AUTO_EXPORT_PATH`, `TOKENOMICS_REDACT_CONTENT` (`1` scrubs prompts/responses) | `adk_agent` / unset / `0` |
| **Custom Rate Card Path** | `TOKENOMICS_MODELS_CONFIG_PATH`, `TOKENOMICS_MODELS_CONFIG`, `$XDG_CONFIG_HOME/adk-tokenomics/models_config.json` | Packaged `models_config.json` |
| **Allowed Web Hosts** | `TOKENOMICS_ALLOWED_HOSTS` *(comma-separated hostnames, `host:port`, or `*.domain`; paired with `--allowed-host`)* | Loopback (`127.0.0.1`, `localhost`, `[::1]`) |

---

## 📁 Repository Structure

```text
adk_tokenomics_plugin/
├── __init__.py              # Public SDK exports (TokenControlTower, ADKTokenomicsPlugin, etc.)
├── __main__.py              # Module execution entrypoint (python -m adk_tokenomics_plugin)
├── tower.py                 # High-level TokenControlTower hub
├── cli.py                   # CLI entry point (web, profile, assert; Phase 2: arena, finops, deploy)
├── cli_web.py               # Embedded HTTP/API server (local JSON traces + BigQuery dual-source)
├── cli_profiler.py          # Turn-by-turn context waterfall & 5 FinOps optimization heuristics
├── cli_arena.py             # Multi-model price vs. quality Pareto evaluator
├── cli_finops.py            # BigQuery fleet-wide FinOps terminal dashboard
├── cli_deploy.py            # Cloud Run deployment helper (adk-tokenomics deploy cloud_run)
├── prompts.py               # Customizable optimization templates & LLM FinOps Advisor builder
├── models_config.json       # Default 1P/3P/Open-Weight model rate card catalog
├── core/
│   ├── config.py            # TokenomicsConfig, OptimizationRulesConfig & BQ identifier validators
│   ├── pricing.py           # PricingEngine (4-bucket cost math, cache ROI & cheapest-model lookup)
│   ├── tracker.py           # TokenTracker + OpenTelemetry (gen_ai.client.cost.usd) bridge
│   └── sinks.py             # InMemorySink and async BigQuerySink streaming sinks
├── middleware/
│   ├── adk_callback.py      # Native Google ADK BasePlugin (ADKTokenomicsPlugin)
│   └── decorator.py         # @track_tokens function/agent wrapper decorator
├── finops/
│   ├── aggregator.py        # Multi-dimensional FinOps matrix & provider aggregator
│   └── bq_schema.py         # BigQuery table & view provisioning helpers
├── testing/
│   ├── assertions.py        # Pytest/unittest & CLI CI/CD FinOps budget assertion gates
│   └── test_case.py         # TokenomicsTestCase base class with isolated track_tokens()
└── web/
    ├── index.html           # Material 3 Dark Slate web observatory UI
    ├── styles.css           # Responsive Material 3 styling & sortable table indicators
    └── app.js               # Live polling, KPI cards, sortable tables & Rate Card editor

examples/
├── README.md                # Beginner-friendly Travel Concierge quickstart (`adk web` + CLI)
├── run_travel_demo.py       # Offline Before/After runner (Unoptimized -> Assert -> Profile -> Optimized)
├── _shared/                 # Travel lookup tools, ADK `SKILL.md` guides & offline trace simulator
├── travel_unoptimized/      # Baseline 3-agent ADK App (Argon everywhere, bloated prompt, 0% cache hit)
└── travel_optimized/        # FinOps-tuned 3-agent ADK App (Flash + Argon tiering, Caching, Compaction, Skills)

agents-cli-extension.yaml    # `agents-cli` extension manifest (`v1alpha1`: `tokenomics`, `eval.budget`, skill)
scripts/
└── run_tokenomics.py        # Zero-install vendored entrypoint for `agents-cli` (`$AGENTS_CLI_EXTENSION_DIR`)
skills/
└── adk-tokenomics/SKILL.md  # Coding-agent skill auto-installed to `.agents/skills/` on `extension add`
```

---

## 📄 License
Apache-2.0
