# Extending Long Horizon

Two ways to change behavior:

1. **Adapt without forking** — teach the running agent through workspace files.
2. **Lift the harness** — fork the interfaces to build your own ADK agent.

This repo is a reference implementation, not a `pip` dependency: the value is the
custom interfaces wired end-to-end on ADK. See [`architecture.md`](architecture.md)
for the overview and [`AGENTS.md`](../AGENTS.md) for the authoritative wiring tables.

## In this doc

- **Adapt without forking** — teach the running agent through workspace files (skills, `scripts/`).
- **Lift the harness** — environment knobs + the `horizon/agent.py` edit points.
- **The custom interfaces → files + insertion points** — where custom code earns its keep, mapped to `agent.py`.
- **The callback ORDER CONTRACT** — the ordered callback lists and why order is the contract.
- **Common swaps** — model/provider, sandbox backend, trimming routes, adding routers/tools/skills.
- **Per-subsystem wiring contracts** — the state keys, ContextVars, and callback slots each subsystem depends on.

---

## 1. Adapt without forking

No plugin framework — two workspace surfaces, hot-reloaded with `/reload`:

- **Skills** — drop `.agents/skills/<name>/SKILL.md` (markdown how-to). Auto-discovered, no
  registration. The background judge writes and patches these as the agent learns.
- **Custom Python** — write `scripts/<name>.py`; the agent runs it via the `bash`
  tool. `bash`/`process` are the integration surface (no extension discovery path).

Per-user config the agent must NOT self-edit lives in the `.lha/` overlay
(`exfil.jsonl`, `policies.jsonl`, `permissions.jsonl`, `routines/<id>.yaml`).

---

## 2. Lift the harness — env + edit points

This is a recipe, not a framework: configure it with environment variables and adapt
it by editing the code. There's no wrapper API to learn.

| Change | How |
|---|---|
| Model / provider | `LHA_ROOT_MODEL` env (or `/model` per session); add a registry entry in `horizon/models/registry.py` |
| System prompt | edit `build_static_instruction()` in `horizon/conversation/system_prompt.py`, wired via `_static_instruction_for()` in `horizon/agent.py` |
| Add / remove a tool | edit the `tools` list in `horizon/agent.py` |
| Add / remove a plugin | edit the `plugins` list in `horizon/agent.py` |
| Sandbox backend | `LHA_ENVIRONMENT_BACKEND=local\|sandbox`, or `set_environment_provider(factory)` for a custom one |
| Add your own route | `app.include_router(...)` on `horizon.fast_api_app.app` |
| Drop a route you don't want | delete the `attach_*` call in `horizon/fast_api_app.py` |
| Session / memory / artifact backend | env URIs (`SESSION_DB_URL`, `AGENT_ENGINE_RESOURCE_NAME`, `LOGS_BUCKET_NAME`, …) |
| Embed without FastAPI | `horizon.fast_api_app.build_runner()` → an ADK `Runner` |

Anything deeper — new callbacks, changing callback order, the App config — is a
`horizon/agent.py` edit (the **order contract** below).

---

## The custom interfaces → files + insertion points

Custom code earns its keep in these interfaces; everything else is either an ADK/Vertex knob
you only configure (compaction, resumability, prefix cache) or an application composed
from the interfaces (routines, scheduler).

| Interface | Files | Wired in `agent.py` at |
|---|---|---|
| Environment interface | `horizon/environment_context.py`, `horizon/environment/`, `horizon/sandbox/` | not a callback — `LHA_ENVIRONMENT_BACKEND` / `set_environment_provider(factory)` |
| Tool guardrails + exfil | `horizon/guardrails/` (`exfil_guard`, `policies_guard`, `permission_guard`, `GuardrailsPlugin`) | `before_tool_callback` list (order-critical) + `plugins=` |
| Per-user secrets | `horizon/secrets/` (`SecretStore`, `secret_env`), `horizon/auth/oauth.py` | not a callback — `LHA_SECRET_BACKEND` / `set_secret_store`; `secret_env` injects into the env, `/lha/secrets` + `/lha/gcp/*` routers |
| Sub-agent delegation + HITL resurfacing | `horizon/subagents/` (`delegate`, `agent`, `delegate_runner`), `SIBLING_AGENT_PLUGIN` | root-agent `tools` + `plugins=` + `subagent_description_callback` (before_model) |
| Self-improvement loop | `horizon/memory/` (`auto_capture`, `review_fork`, `skill_curator`, `dream_review`) | `after_agent_callback` list + `PreloadMemoryTool()` in tools + nightly `/scheduler/dream-review` |
| 3-tier system prompt | `horizon/conversation/` (`system_prompt.py`, `reminders.py`) | `system_prompt_assembly_callback` + `reminder_injection_callback` (before_model) |

> **Not interfaces — ADK/Vertex knobs you only configure:** compaction
> (`App(events_compaction_config=EventsCompactionConfig(summarizer=HorizonSummarizer(...)))`
> — Horizon supplies only the summarization prompt + banner), resumability
> (`ResumabilityConfig(is_resumable=True)`), and prefix caching (`ContextCacheConfig`).
> Full features that *compose* the interfaces — routines, the scheduler — live in their own
> docs + the [architecture tree map](architecture.md#backend-tree-map--where-to-start).

---

## The callback ORDER CONTRACT

`horizon/agent.py:_build_app_object` registers callbacks as **ordered lists**. Order
is the contract: callbacks run top-to-bottom and later entries read state mutated by
earlier ones. To add a guardrail/callback, insert it at the correct labeled position
— do not append blindly. Plugins (`IterationBudgetPlugin`, `SIBLING_AGENT_PLUGIN`,
`GuardrailsPlugin`) run **before** agent-level callbacks at each hook.

| Stage | Run order (top → bottom) |
|---|---|
| `before_agent_callback` | `on_session_start_callback` → `bind_session_skills_callback` |
| `before_model_callback` | `select_model_callback` → `prune_tool_outputs_callback` → `redact_artifact_urls_callback` → `_slash_command_dispatcher` → `system_prompt_assembly_callback` → `reminder_injection_callback` → `subagent_description_callback` |
| `before_tool_callback` | `before_tool_log_callback` → `exfil_guard` → `policies_guard` → `permission_guard` |
| `after_tool_callback` | `skill_telemetry_callback` → `tool_call_log_callback` |
| `after_agent_callback` | `auto_capture_callback` → `skill_curator_callback` → `review_fork_callback` |

Why order matters (examples): `select_model_callback` runs first in before_model so everything downstream sees the resolved model name and threshold; `exfil_guard` runs before `policies_guard` so a secret-bearing call is blocked regardless of destructive rules; `permission_guard` runs **last** in before_tool so the security guards keep hard-deny power and it only decides allow/ask for survivors. The canonical annotated
table is in [`AGENTS.md`](../AGENTS.md) ("ADK Callback Wiring").

---

## Common swaps

### Model / provider

The registry in `horizon/models/registry.py` maps a name → an ADK `BaseLlm`. Add an
entry (e.g. `LiteLlm(...)` for a non-Vertex provider) and pass its key as `model=`.
Per-session switching uses `/model <name>` (writes `selected_model`, read by
`select_model_callback`). Pinned: the `web_research` sub-agent + compaction summarizer
use `gemini-3.7-flash` — don't change without being asked.

> **Minimal install:** backends build lazily and `DispatchingLlm` holds the registry
> without materializing it (`backends` is `SkipValidation`), so a string `model=` only
> builds the backend it routes to — the default agent builds without touching the
> others. Adding a registry entry for a non-Vertex provider needs that provider's
> package installed. To wire a non-registry `BaseLlm`, edit `_resolve_root_model` in
> `horizon/agent.py` — but that bypasses `/model` switching.

### Sandbox backend (e.g. GKE)

Subclass Horizon's **`Environment`** (`horizon.environment`) and install a factory
before serving — tools call the env through the `environment_context.py` ContextVar,
never the host directly. `Environment` is a superset of ADK's `BaseEnvironment`:
beyond `working_dir`/`execute`/`read_file`/`write_file` you also implement
`list_directory`, `delete_file`, `make_dir`, `download_zip`, `upload_zip`, and
`spawn_process` (returns a `ProcessHandle` from `horizon.environment.process`), and
set the capability flag `on_host_fs` (False for a remote backend). For short-lived credentials, override
`refresh_auth() -> bool` (called per turn; return `False` when the instance is gone
so the orchestrator evicts) — the light `set_environment_provider` hook gets refresh
too, no provider required. Callers dispatch by method/capability, so a
correctly-implemented backend routes without any `isinstance` edits:

```python
from horizon.environment import Environment
from horizon.environment.process import ProcessHandle
from horizon.environment_context import set_environment_provider


class GkeEnvironment(Environment):
    on_host_fs = False

    async def execute(self, command: str, *, timeout: float | None = None): ...
    async def read_file(self, path): ...
    async def write_file(self, path, content: str | bytes): ...
    async def list_directory(self, path, *, limit): ...
    async def make_dir(self, path): ...
    async def delete_file(self, path, *, recursive=False): ...
    async def download_zip(self, path): ...
    async def upload_zip(self, path, data): ...
    async def spawn_process(self, command, *, cwd=None, env=None) -> ProcessHandle: ...

    async def refresh_auth(self) -> bool:
        # optional: re-mint a short-lived platform token each turn;
        # return False once the backend is gone so the orchestrator evicts.
        return True


set_environment_provider(lambda user_id: GkeEnvironment(user_id, ...))
```

**Full lifecycle (provisioning/reattach/snapshot/upgrade/auth): implement a
`SandboxProvider`** (`horizon.sandbox.provider`) and register it with
`set_sandbox_provider(provider)`. `session_start.py` orchestrates the per-user env
cache / locks / ContextVar / eviction over whichever provider is active and calls
its `build_environment` / `build_routine_environment` / `provision_upgrade` /
`snapshot_and_prune` / `provisioning_status` / `will_provision` (per-turn auth is
env-owned via `Environment.refresh_auth`, not a provider method). `VertexSandboxProvider` (composes
`sandbox/lifecycle.py`) and `LocalProvider` are the built-ins selected by
`LHA_ENVIRONMENT_BACKEND`. Use `set_environment_provider` (above) when you only need
to swap the per-session env and keep no provisioning; use `set_sandbox_provider`
when your backend has its own reattach/snapshot/upgrade lifecycle.

```python
from horizon.sandbox.provider import SandboxProvider  # a typing.Protocol
from horizon.environment_context import set_sandbox_provider

set_sandbox_provider(GkeSandboxProvider())
```

### Trim routes

All routers mount by default (A2A + `/lha/*` + `/feedback` + OAuth + `/scheduler/*`).
To ship a subset, delete the `attach_*` calls you don't want in
`horizon/fast_api_app.py`. Removing a route that injects credentials (`secrets`,
`oauth`) or runs unattended (routines) also removes that surface; the runtime
guards (exfil, permission, per-user isolation) are unaffected. See
[`security-model.md`](security-model.md).

### Add routers / tools / skills

- **Routers:** `app.include_router(my_router)` on `horizon.fast_api_app.app`.
- **Tools:** a plain typed function with a docstring is auto-wrapped as a `FunctionTool`
  — the docstring is what the model reads, so write it for the model. Add the function
  (or an ADK tool instance) to the `tools` list in `horizon/agent.py` (import the
  function/instance, not the module).
- **Skills:** no code — drop a `SKILL.md` (see §1).

---

## Per-subsystem wiring contracts

If you lift a subsystem, these are the session.state keys, ContextVars, and callback
slots it depends on. Sources: [`AGENTS.md`](../AGENTS.md) "State keys" + "ADK Callback Wiring".

### `memory/` — self-improvement loop on Memory Bank
- **Tools:** `PreloadMemoryTool()`, `memory` (add and search — the former standalone
  `session_search` is now `memory(action='search', ...)`) (root agent `tools`).
- **Callbacks:** `auto_capture_callback`, `skill_curator_callback`, `review_fork_callback` (after_agent).
- **ContextVar:** `compaction_context` (memory-service handles, set in `on_session_start_callback`) — the summarizer's pre-compaction flush reads it.
- **Service:** resolved from env — `InMemoryMemoryService` under `USE_IN_MEMORY_SESSION`, `VertexAiMemoryBankService` when an Agent Engine resource is configured. In-memory ⇒ cross-session memory + dream-review no-op.
- **Scheduler/env:** nightly `/scheduler/dream-review`; `LHA_DREAM_REVIEW`, `LHA_MEMORY_CONSOLIDATION`, `LHA_DREAM_*`.

### `sandbox/` — environment interface
- **ContextVar:** `Environment` (`horizon/environment/base.py`, a superset of ADK's `BaseEnvironment`) in `environment_context.py`; selected by `LHA_ENVIRONMENT_BACKEND` (string) or `set_environment_provider(factory)` (custom backend). Callers dispatch by method/capability flag (`on_host_fs`), never `isinstance`.
- **Env:** `LHA_ENVIRONMENT_BACKEND`, `LHA_RUNTIME_IMAGE`, `LHA_SANDBOX_*`.

### `routines/` — unattended recurring tasks
- **ContextVars (async-local):** `set_routine_run` / `set_routine_secret_scope` / `set_headless_mode`, set by `scheduler/routine_tick_endpoint._fire_routine`, reset in `finally`.
- **Store:** `RoutineStore` (`LHA_ROUTINE_STORE=memory|postgres`); manifests in `.lha/routines/<id>.yaml`.
- **Capability:** `scheduler` (gates routes + tick). **Tool:** `routine` (authoring).

### `guardrails/` — tool guardrails
- **Callback slots (before_tool, order-critical):** `exfil_guard` → `policies_guard` → `permission_guard`.
- **Plugin:** `GuardrailsPlugin` (before_model halt consumer, after_model no-progress, after_tool repeated-failure).
- **session.state:** `halt_reason`, `approval_mode` (`/yolo`), `permission_grants`.
- **Overlays:** `.lha/exfil.jsonl`, `.lha/policies.jsonl`, `.lha/permissions.jsonl`. Headless children pass `ask_is_deny=True`.

### `secrets/` — per-user secrets
- **Interface:** `SecretStore` Protocol (`LHA_SECRET_BACKEND=secretmanager|memory`, `set_secret_store` to override); resolved + scoped via `secret_env` (`secrets/inject.py`), injected into the env each turn — the model sees the name, never the value.
- **OAuth:** `horizon/auth/oauth.py` (`/lha/gcp/*` Connect-Google buttons) writes tokens as per-user secrets.
- **Routines:** `set_routine_secret_scope` filters resolved secrets to a routine's declared names (the blast-radius boundary).

### `context/` — compression + per-turn steering
- **Callbacks (before_model):** `select_model_callback` (stamps model + compaction threshold), `prune_tool_outputs_callback`, `redact_artifact_urls_callback`.
- **App config:** `events_compaction_config=EventsCompactionConfig(summarizer=HorizonSummarizer(...))`.
- **ContextVar:** `compaction_context`. **Env:** `LHA_PRUNE_TOOL_OUTPUTS`, `LHA_COMPACTION_WINDOW_FRACTION`.

### `subagents/` — delegation
- **Tool:** `subagent` (`horizon/subagents/subagent.py`), a single dispatcher over two still-internal callables: blocking `delegate()` (resumable child that resurfaces approvals) by default, or fire-and-forget `agent()` (`horizon/subagents/spawn.py`, headless) when `background=True`.
- **Plugin:** `SIBLING_AGENT_PLUGIN`. **Callback:** `subagent_description_callback` (before_model) rewrites the delegation menu.
- **permission_guard:** `SUBAGENT_TOOLS` are exempt at spawn; a blocking `subagent` call resurfaces risky-op approvals, a background one stays headless (`ask_is_deny`).

### Static + volatile system prompt (`conversation/`)
- **Construction:** `build_static_instruction()` assembles the process-wide-constant prefix once at App-build time, wired as `Agent(static_instruction=...)` via `_static_instruction_for()` in `horizon/agent.py` — ADK's own request processor places it ahead of every callback.
- **Callbacks (before_model):** `system_prompt_assembly_callback` now only appends the per-cwd project-context tier (`.horizon.md`/`AGENTS.md`) to `system_instruction`, keeping it in the context cache; `reminder_injection_callback` appends the volatile tier (iteration/error/date + env hint + secrets) as a trailing `<system-reminder>` on the message tail, keeping the cached prefix byte-stable.
- **Override:** edit `build_static_instruction()` in `horizon/conversation/system_prompt.py` (the base system prompt).

---

## Where to go next

- [`README.md`](../README.md) — what Horizon is and the feature tour
- [`architecture.md`](architecture.md) — the architecture map / overview
- [`configuration.md`](configuration.md) — every env var and dependency extra
- [`commands.md`](commands.md) — the slash-command catalog
- [`AGENTS.md`](../AGENTS.md) — the authoritative wiring tables + conventions (maintainer view)
