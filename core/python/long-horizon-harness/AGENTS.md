# Long Horizon — agent guide

Self-improving long-horizon agent on Google ADK + Vertex AI.

This file has two halves. **[Studying the recipe](#what-this-recipe-teaches)** is the
entry point: scan the recipe table, jump to the one pattern you want, and read the real
function that implements it. **[Maintaining the code](#maintaining-the-code)** is the
exhaustive view — conventions, callback order, state keys, env vars — for when you are
changing the repo rather than lifting from it.

> **The code is the documentation.** Each pattern below points at the actual production
> function that implements it — not a parallel toy example that would drift.

Deep-dives live in [`docs/architecture.md`](docs/architecture.md) (the map),
[`docs/configuration.md`](docs/configuration.md) (env vars),
[`docs/extending.md`](docs/extending.md) (extension points),
[`docs/commands.md`](docs/commands.md) (slash commands),
[`docs/context-budget.md`](docs/context-budget.md) (per-turn prefix, prefix cache, the
four bounds on context growth), and
[`docs/security-model.md`](docs/security-model.md) (per-layer security). Don't duplicate
those here — link to them.

## What this recipe teaches

Horizon leans on ADK + Vertex primitives; the **six interfaces** below are where custom code
genuinely earns its keep — a real Protocol, ContextVar, or ordered callback chain, the
densest and most liftable, so start here. Each is self-contained:

1. **Environment interface** — tools call a pluggable `Environment`, not the host.
2. **Tool guardrails + exfil/egress** — block/ask on risky tool calls.
3. **Per-user secrets** — act with the user's own credentials, unseen by the model.
4. **Sub-agent delegation + HITL resurfacing** — one `subagent` tool, blocking by default or fire-and-forget with `background=True`; a child bubbles a risky-op approval to the human and resumes.
5. **Self-improvement loop** — write facts/skills to memory between turns.
6. **3-tier system prompt** — constant prefix on ADK's `static_instruction`, per-cwd context tier, per-turn volatile tail.

These are *interfaces*, not knobs: things like **compaction**, **resumability**, and the
**prefix cache** are mostly ADK/Vertex config Horizon only tunes (compaction ships a summarization prompt + banner over
ADK's `LlmEventSummarizer`), and features like **routines** and the **scheduler** are
*applications* composed from the interfaces. Those — plus other subsystems — are curated in
[**Beyond the six**](#beyond-the-six--other-subsystems-worth-studying), and
[`docs/architecture.md`](docs/architecture.md)'s Backend tree map is the complete
per-subsystem index.

## Recipe table — where to study + how to lift

The **Start-here** column names the exact real function to read — open it; that
production code is the lesson.

| Interface | Start-here (real function) | Transferable vs Horizon-specific | Deep-dive |
|---|---|---|---|
| Environment interface | `horizon/environment/base.py` → `Environment`; `horizon/environment_context.py` → `active_environment()` / `set_environment_provider()` | Copy: an `Environment` (Horizon's contract — a superset of ADK's `BaseEnvironment` adding `list_directory`/`delete_file`/`make_dir`/`download_zip`/`upload_zip`/`spawn_process` (returns a `ProcessHandle` from `horizon/environment/process.py`) + capability flags `on_host_fs` + a per-turn `refresh_auth()`) behind a ContextVar; tools call `active_environment()` and dispatch by method/capability, never `isinstance` or the host (zero concrete-class `isinstance` remain). Per-turn auth is **env-owned** (`refresh_auth`), so the light `set_environment_provider` hook alone suffices for a backend with short-lived tokens. Full provisioning/reattach/snapshot/upgrade lives behind a second interface — `SandboxProvider` (`horizon/sandbox/provider.py`), `VertexSandboxProvider` | `LocalProvider`, overridable via `set_sandbox_provider`. Specific: the Sandbox REST backend (`horizon/environment/sandbox.py`) + its provisioning subsystem (`horizon/sandbox/`); also the per-session focus lens (`horizon/workspace_window.py` → `resolve_in_window`) — a default/lens, not a security boundary. | [`docs/sandbox-lifecycle.md`](docs/sandbox-lifecycle.md) |
| Tool guardrails | `horizon/guardrails/__init__.py` (package docstring = the **contract**) → `exfil_guard()` (worked example) | Copy the contract the package docstring states: a `before_tool` callback `(*, tool, args, tool_context)` returning `None` to allow or a dict-with-`error` to block, added to `horizon/agent.py`'s `before_tool_callback` list. Specific (skip when lifting): `exfil_guard`'s ~570 lines are exfil-detection heuristics; the three-layer exfil/policy/permission chain (`guardrails_plugin.py` is the sibling halt plugin). Layer D — the interactive ask-layer that runs last (argv classifier so a benign segment can't smuggle a gated one) — is `horizon/guardrails/permission_guard.py` → `permission_guard`. | [`docs/security-model.md`](docs/security-model.md), [`docs/permission-model.md`](docs/permission-model.md) |
| Per-user secrets | `horizon/secrets/store.py` → `SecretStore` Protocol (`SecretManagerStore` \| `InMemorySecretStore`, selected by `LHA_SECRET_BACKEND`, overridable via `set_secret_store`); `horizon/secrets/inject.py` → `secret_env` / `set_routine_secret_scope`; OAuth `horizon/auth/oauth.py` → `attach_gcp_oauth_routes` / `sign_state` / `verify_state` | Copy: a per-user `SecretStore` behind a Protocol + env selector, resolved and scoped behind a single env-injection interface so the agent acts with the user's credentials while the model sees only the name; access-token-only OAuth with an HMAC-signed `state` cross-checked against IAP identity (no refresh token stored). Specific: Secret Manager + the vendor `NotFound`/`AlreadyExists` translation (a fake needs zero GCP imports) + the Connect-Google surface. | [`docs/security-model.md`](docs/security-model.md) |
| Sub-agent delegation + HITL resurfacing | `horizon/subagents/subagent.py` → `subagent()` (a single dispatcher over two still-internal callables: blocking `delegate()` in `subagents/delegate.py`, fire-and-forget `agent()` in `subagents/spawn.py`; resumable child driver `subagents/delegate_runner.py` → `drive_child`) | Copy: one `subagent` root tool, blocking by default or fire-and-forget with `background=True`, each child with its own isolated context window; the blocking path drives a resumable child that pauses on a risky-op approval, bubbles it to the human, and resumes from the stored `FunctionResponse` — durable HITL without re-running the turn. Specific: the resurfacing bubble budget + `ask_parent` escalation. | — |
| Self-improvement loop | `horizon/memory/auto_capture.py` → `auto_capture_callback()` | Copy: an `after_agent` callback calling `callback_context.add_session_to_memory()` (an ADK `CallbackContext` primitive) for write-back, plus a `PreloadMemoryTool` in the root agent's `tools` for prefetch = cross-session recall (both wired in `horizon/agent.py`). Backend-specific memory access (profiles + list-all) is confined to one interface — `horizon/memory/adapter.py` (`MemoryAdapter` Protocol + `memory_adapter()` factory), so callers name no concrete ADK service class and a non-Vertex backend degrades cleanly. Specific: dream-review, judge fork, and the skills system — auto-discovery from `SKILL.md` (`horizon/tools/skill_reload.py` → `bind_session_skills_callback`) plus the promote/demote curator (`horizon/memory/skill_curator.py` → `skill_curator_callback`). | [`docs/memory.md`](docs/memory.md) |
| 3-tier system prompt | `horizon/conversation/system_prompt.py` → `build_static_instruction()` (wired as `Agent(static_instruction=...)` in `horizon/agent.py`), context tier via `make_system_prompt_callback()` (volatile tail in `conversation/reminders.py`) | Copy: the constant tier rides ADK's own `Agent.static_instruction` — a pure function of `(tool_names, model_name, has_code_executor)`, built once at App-build time so ADK's request processor (`flows/llm_flows/instructions.py`) places it ahead of every callback, deterministically, instead of horizon hand-rolling a per-session `session.state` cache. The context tier (per-cwd project file) still rides a `before_model_callback` since it varies by session; the volatile tail (iteration/error/date + the env hint + secrets line) rides trailing `<system-reminder>` `Content` so the cached prefix stays byte-stable. Specific: the soul/skill tiers. | [`docs/context-budget.md`](docs/context-budget.md) |

The interfaces above are taught by Horizon's **real code** — nothing to run, nothing
that drifts. To run and adapt the recipe rather than just study it, see
[`docs/quickstart.md`](docs/quickstart.md) (the smallest embeddable harness) and
[`docs/extending.md`](docs/extending.md) (a custom `Environment` backend, and
adding a route / tool / skill).

## Beyond the six — other subsystems worth studying

The six interfaces are where Horizon writes the *most* custom code — not all of it. Some entries
here are ADK/Vertex **knobs** Horizon only tunes (compaction, resumability); some are
**applications** that compose the interfaces (routines, scheduler); the rest are smaller or more
specialized subsystems. Each still carries a production lesson worth lifting. A curated
shortlist; for the complete per-subsystem index see
[`docs/architecture.md`](docs/architecture.md)'s [Backend tree map](docs/architecture.md#backend-tree-map--where-to-start).

| Topic | Start-here (file → symbol) | Why worth studying | Deep-dive |
|---|---|---|---|
| A2A + Gemini Enterprise interop | `horizon/a2a/executor.py` → `_StreamDedupConverter` (+ `_surface_artifact_links`, `_strip_fake_artifact_links`); transport `horizon/a2a/routes.py` → `attach_a2a_routes` | One A2A converter satisfies two non-conformant clients (Gemini Enterprise vs. the web) at once — dedupe streamed text, reshape tool chips, lift artifact links GE buries, strip model-fabricated links. | — |
| Model routing interface | `horizon/models/dispatcher.py` → `DispatchingLlm`; `horizon/models/registry.py` → `_MODELS` (`ModelDescriptor`) + `model_capabilities`; `horizon/models/capabilities.py` → `ModelCapabilities` | Copy: one `BaseLlm` holds every registered backend + a per-backend capability descriptor (media limits + an optional `prepare_contents` content hook) instead of `isinstance`/name-gating. Ships Gemini-only (`gemini-3.7-flash` default + `gemini-3.1-pro-preview`, both via `/model`); adding a model or provider (e.g. a `LiteLlm` entry) is one `_MODELS` table entry. | — |
| Resumability (an ADK knob) | `horizon/agent.py` → `ResumabilityConfig(is_resumable=True)` (one line; durable persistence is Agent Platform Sessions) | Config, not an interface: it's the ADK/Vertex primitive the Sub-agent-delegation interface's child driver (`delegate_runner.py` → `drive_child`) builds on to pause on a risky-op approval and resume from the stored `FunctionResponse`. The custom value is that child driver (interface 4), not resumability itself. | — |
| Routines (unattended cron, isolated sandbox) | `horizon/routines/tools.py` → `_create`; fire path `horizon/scheduler/routine_tick_endpoint.py` → `routine_tick` / `_fire_routine`; isolation `horizon/routines/run_context.py` | A recurring task runs headless in a fresh, disjoint `lhart-` sandbox scoped to only its declared secrets, with non-shell approvals auto-denied — the blast-radius design is the lesson. | [`docs/routines.md`](docs/routines.md) |
| Scheduler (routine fire turns as persisted chats / dream-review / snapshot) | `horizon/scheduler/sessions.py` → `create_scheduled_session`; fire `horizon/scheduler/routine_tick_endpoint.py` → `routine_tick` / `_fire_routine`; `horizon/scheduler/dream_review_endpoint.py` → `dream_review_tick`; `horizon/scheduler/snapshot_endpoint.py` → `snapshot_tick` | A scheduled turn drives the *same* shared A2A handler against a pre-tagged session so it records a real Task/history — driving the runner directly would leave the UI blank. | — |
| Context compression (an ADK knob) | `horizon/context/summarizer.py` → `HorizonSummarizer` (subclasses ADK's `LlmEventSummarizer`, an `EventsCompactionConfig` hook) | Mostly config, not an interface: ADK owns the trigger/retention/lifecycle; Horizon supplies only a structured summarization prompt + a REFERENCE-ONLY banner. Its one custom idea — a pre-compaction memory fork (`spawn_flush_fork`) so durable facts land before a lossy summary — is really the self-improvement loop. | [`docs/memory.md`](docs/memory.md) |
| DB connection resilience | `horizon/infrastructure/db_resilience.py` → `retry_on_disconnect` / `resilient_engine_kwargs` / `is_transient_disconnect` | Why `pool_pre_ping` is not enough (it validates only at checkout) and how every op wraps a transient-only retry to survive a Cloud SQL failover mid-query. | — |
| Sandbox lifecycle | `horizon/sandbox/lifecycle.py` → `find_latest_user_sandbox` (version-agnostic reattach) / `snapshot_and_prune_user` / `restore_sandbox_from_snapshot` | Version-scoped identity but version-agnostic reattach (a rollout never wipes installed CLIs); snapshot/restore for TTL survival — the lifecycle math is the hard part. | [`docs/sandbox-lifecycle.md`](docs/sandbox-lifecycle.md) |
| FastAPI serving surface | `horizon/fast_api_app.py` → `_build_app` (mounts A2A + all `/lha/*` + `/scheduler/*`) / `build_runner` (Runner, no FastAPI) | Env-driven serving over the agent; ship a subset by deleting `attach_*` calls. What each route exposes is the security story. | [`docs/security-model.md`](docs/security-model.md) |

Honorable mention — **artifact signed URLs + model redaction**: `horizon/tools/_artifact_links.py` → `artifact_url` / `_signed_blob_url` (a V4 signed URL via IAM SignBlob, no private key) paired with `horizon/context/artifact_url_redaction.py` → `redact_artifact_urls_callback` (the model reads a placeholder, never the credentialed blob).

Still not exhaustive: [`docs/architecture.md`](docs/architecture.md)'s [Backend tree map](docs/architecture.md#backend-tree-map--where-to-start) lists every subsystem with its own start-here file — read this section for the highlights, that map for full coverage.

## Study order

1. Skim [`docs/architecture.md`](docs/architecture.md) — the map and the construction/execution flow.
2. Pick **one** pattern from the table above.
3. Open its **real function** (the Start-here column); fan out to the supporting files the architecture map lists.
4. Read its **deep-dive** doc.
5. Only then read [Maintaining the code](#maintaining-the-code) below for exhaustive wiring (callback order, state keys).
6. To run/adapt the recipe in your own app, read [`docs/extending.md`](docs/extending.md) and [`docs/quickstart.md`](docs/quickstart.md).

## What to ignore when studying

The chat UI in `web/` (a Vite SPA + Express proxy behind IAP; see `web/README.md`) is a
real part of the repo — read it if the frontend is what you're after; it's just outside the
ADK harness interfaces this guide teaches. When studying the *interfaces*, you can skip:

- `terraform/` — deploy infra; relevant only if you're running Horizon as a starter.
- `tests/eval/` — LLM-behavior validation, not pattern source. (`tests/unit` + `tests/integration` show contracts.)
- Generated/large files: `uv.lock`, `*.db`, `.venv/`, `node_modules/`.

## agents-cli recipe metadata

- **name:** `horizon`
- **one-liner:** Self-improving, long-horizon ADK agent on Google Agent Platform — per-user sandbox, cross-session memory, tool guardrails, and a between-turns self-improvement loop.
- **keywords:** long-horizon, self-improving, memory bank, sandbox, guardrails, exfil, egress, sub-agents, delegation, routines, scheduler, a2a, resumable, skills, vertex, agent runtime, oauth, secrets, model-routing, db-resilience
- **key files:** `horizon/agent.py`, `horizon/fast_api_app.py`, `docs/quickstart.md`, `docs/architecture.md`, `AGENTS.md`

---

# Maintaining the code

Everything below is the maintainer view: conventions and exact wiring. Read it when you
are changing the repo, not when you are lifting a pattern out of it.

## Keep this file current

Every future agent session reads this before touching code. After **non-trivial structural changes** — new top-level dirs, subsystems, tools, callback re-wiring, env-var/model/lint/test/port/route changes — **update this file in the same commit**. Drift here costs every future session.

## Development Rules

1. **Test-first.** Tests/evalsets before production code. No code without a failing test pointing at it.
2. **Never assert on LLM output content in pytest.** Behavior validation → `tests/eval/datasets/*.json` (or `tests/eval/evalsets/*.json` for the three replay suites). Pytest is for code correctness (types, contracts, persistence, tool I/O).
3. **Use ADK primitives** (Runner, Session service, Memory Bank). Custom code earns its keep only in the **six interfaces**: environment interface, tool guardrails (exfil/policy/permission), per-user secrets, sub-agent delegation + HITL resurfacing, self-improvement loop on Memory Bank, 3-tier system prompt. Everything else is either an ADK/Vertex **knob** you only configure (compaction via `EventsCompactionConfig`, resumability, prefix cache) or an **application** composed from those interfaces (routines, scheduler). See [`docs/architecture.md`](docs/architecture.md#where-custom-code-earns-its-keep).
4. **Memory model.** ADK Memory Bank is the only cross-session store — no custom SQLite. `InMemoryMemoryService` in tests, `VertexAiMemoryBankService` in deploy. The per-user profile is a **native Structured Profile** (schema in `horizon/infrastructure/memory_config.py`, applied by `scripts/provision_agent_engine.py`): dream-review writes it via `memories.generate`, the live agent reads it via `memories.retrieve_profiles` (`horizon/memory/user_profile.py`) — not verbatim markers (Memory Bank extracts/consolidates). The same `memories.generate` call consolidates the user's **general** memories (dedupe + contradiction reconciliation; on by default, gated by `LHA_MEMORY_CONSOLIDATION`; dream pass surfaces `created`/`updated`/`deleted` counts).
5. **Respect the scope table.** Out-of-scope: alternate gateways (Telegram/Discord/Slack/WhatsApp/Signal), sandbox backends other than the managed Sandboxes backend + local fallback, browser/computer-use/image-gen/video-gen/TTS/voice tools, MCP server, TUI, ACP.

## Project Layout

```
lha/
├── horizon/                      # importable Python package (`import horizon`); outer repo dir stays `lha/`
│   ├── agent.py                  # THE REAL AGENT: root_agent + App(...) full tool/callback/plugin wiring + module state (SIBLING_AGENT_PLUGIN, _SKILL_TOOLSET). Owns _build_app_object; ADK Runner entrypoint reads root_agent/app here.
│   ├── fast_api_app.py           # THE SERVED APP: _build_app() (env-driven — services + sandbox + all routers mounted) + build_runner() (ADK Runner, no FastAPI) + service/task-store URI resolution + resilient wrappers. Lazy `app` via __getattr__ (bare import stays offline).
│   ├── environment/               # Environment interface: the contract + ALL backends + process representation. base.py (ABC: full runtime I/O contract + capability flags on_host_fs + refresh_auth default), process.py (ProcessHandle protocol + BackendGoneError — the leaf that owns the process vocabulary, killing the old environment↔tools.processes cycle), local.py (LocalEnvironment) + local_process.py (LocalProcessHandle) + registry.py (ProcessRegistry), sandbox.py (SandboxEnvironment — self-refreshing Agent Platform sandbox REST client) + sandbox_process.py (SandboxProcessHandle). SandboxEnvironment is exposed lazily from the package (bare import stays httpx-free); horizon.sandbox.SandboxEnvironment stays importable via re-export.
│   ├── environment_context.py    # ContextVar Environment interface (local vs sandbox) + set_environment_provider override
│   ├── workspace_window.py       # Per-session workspace focus lens (resolve_in_window), used by path tools
│   ├── a2a/                      # A2A adapters: routes (JSON-RPC + agent card), executor (task-tracking + the SINGLE A2A converter `_StreamDedupConverter`, forced for ALL clients via `force_new_version=True` — both GE and web take ADK's new-impl path. On a2a 1.x it is built on ADK's version-agnostic `google.adk.a2a._compat` (proto part/event builders — `is_text_part`/`data_part_dict`/`make_*`) and enforces create-before-append (1.x rejects `append=True` for an artifact never created `append=False`, which the tool/link surfacing can orphan — see `_enforce_artifact_lifecycle`). Strips the redundant streamed-text aggregate so non-conformant clients e.g. GE don't double the reply; moves function call/response DataParts from artifact-updates onto working status messages (the web reads `adk_type` to render the rich tool chip), prefixing each tool name with `horizon__` (GE's thinking timeline drops any name not matching `<agent>__<action>`; the web strips it in `mapPart`; framework HITL names `clarify`/`adk_request_confirmation` stay flat); lifts saved-artifact links into a `📎 [name](url)` text status tagged `lha_ge_label` — the web renders the artifact inline from its FilePart and skips the link, GE keeps it — from the `artifact` function_response, since GE buries data parts and the model can't echo a signed URL; also strips fabricated `attachment:`/`sandbox:` artifact links the model invents (the real URL is redacted from its view, no client renders these schemes — `_strip_fake_artifact_links`). A streamed function_call is emitted twice (partial + final) and surfaced twice; the web dedupes tool chips by callId in `chat-segments.ts`. HITL: the new-impl executor emits the `adk_request_confirmation` as a `final=true` `input-required` status, which A2A stores in `task.status.message` (NOT history), so `task-to-segments.ts` reads `status.message` on input-/auth-required tasks or the card vanishes on the post-turn history refetch), user_converter (authenticated user_id, no synthetic A2A_USER_*), datapart. Routes (a2a 1.x) use `add_a2a_routes_to_fastapi` + a v0.3 `AgentInterface` `card_modifier` so GE's Discovery-Engine validator (needs top-level `url`/`protocolVersion`) accepts the card while 1.0 clients use `supportedInterfaces`; `attach_a2a_routes` builds the card + handler and returns it. The old `ResilientRequestHandler` is GONE — the empty-SSE-on-error hang it patched is fixed upstream (a2a 1.x v0.3 `JSONRPCAdapter` turns a mid-stream raise into an SSE error frame), so the stock `DefaultRequestHandler(agent_card=)` is used
│   ├── api/                      # FastAPI routers for /lha/* + /feedback: sessions, state, tasks, memories, sandbox, processes (GET /lha/processes = list running sandbox processes + DELETE /lha/processes/{sid} = kill; peek-only, never provisions), secrets, routines (GET /lha/routines = read-only routine list, behind `scheduler` cap), uploads, feedback (POST /feedback = origin:"user")
│   ├── auth/                     # identity.py (request-scoped identity) + oauth.py (/lha/gcp/* "Connect Google" buttons)
│   ├── builtin_skills/           # In-repo skills (find-skills, google-workspace, policy, bootstrap-google-tools, routines). Auto-discovered from <name>/SKILL.md, no registration. User skills live under <workspace>/.agents/skills/<name>/ (the `npx skills add` staging location + ecosystem standard; agent authors/curates there too), mirrored to the host catalog by skill_loader.mirror_user_skills_to_host. No separate skills/ root.
│   ├── commands/                 # Slash commands (/model, /grant, /permissions, /yolo, /dream-review, /reload, /sandbox-upgrade, /workspace, /routines) + dispatcher. Generated catalog: docs/commands.md.
│   ├── context/                  # HorizonSummarizer (EventsCompactionConfig) + compaction_context ContextVar
│   ├── conversation/             # session_start, system_prompt (build_static_instruction + context tier), IterationBudgetPlugin, soul_loader
│   ├── guardrails/               # GuardrailsPlugin (halt/no-progress/repeated-failure) + policies (Layer C, `policies_guard(ask_is_deny=...)` consults `command_safety.py` argv classifier + seed/overlay) + exfil_guard (Layer A, defaults exfil_config.py, overlay .lha/exfil.jsonl, /grant bypass) + permission_guard (Layer D interactive ask-layer, runs last) + permission_rules (.lha/permissions.jsonl) + command_classify + command_safety (argv tokenizer+structural classifier, returns "deny"|"ask"|None) + _regex_safety (tenant-authored regex validation)
│   ├── memory/                   # memory (add_memory_tool.py), auto_capture, review_fork, dream_review, user_profile, skill_curator, sibling_agent_plugin, memory_list; adapter.py = the single memory-backend interface (`MemoryAdapter` Protocol + `memory_adapter()` factory: `VertexMemoryAdapter`|`InMemoryMemoryAdapter`|`NoopMemoryAdapter`, owns ALL `isinstance(VertexAiMemoryBankService)` + `_get_api_client`/`_agent_engine_id`/`_session_events` reaches)
│   ├── routines/                 # Unattended recurring tasks: manifest (RoutineManifest + parse + write_routine_via_env writer), tools (routine test/create/list/cancel), run_context, isolation (shared 3-ContextVar CM + HEADLESS_PREAMBLE), run_once (synchronous test run via routine(action="test")). Runtime schedule row + fire path live under scheduler/. Doc: docs/routines.md.
│   ├── sandbox/                  # Vertex provisioning/lifecycle subsystem (the Environment backend itself moved to horizon/environment/sandbox.py). provider.py (SandboxProvider protocol + VertexSandboxProvider | LocalProvider — provisioning/reattach/snapshot/upgrade; per-turn auth is now env-owned) + lifecycle.py (Agent Platform sandboxes SDK wrappers); runtime/ holds the in-container FastAPI shim (Dockerfile + protocol.py + server.py, pushed as LHA_RUNTIME_IMAGE). Doc: docs/sandbox-lifecycle.md.
│   ├── scheduler/                # Cloud Scheduler (auth, dream-review/snapshot endpoints) + routines runtime (routine_store, routine_postgres_store, cron.py, routine_tick_endpoint). See "Scheduler" below + docs/routines.md.
│   ├── secrets/                  # Per-user secret store behind a `SecretStore` Protocol (`SecretManagerStore` GCP + TTL cache | `InMemorySecretStore`, selected by `LHA_SECRET_BACKEND` in get_secret_store, overridable via set_secret_store) + env injection + dotenv parser; /lha/secrets router in api/secrets.py
│   ├── subagents/                # web_research.py (web_research subagent, gemini-3.7-flash, google_search is Gemini-only) + subagent.py (single dispatcher, first-class root-agent tool) over two still-internal callables: delegate.py (blocking) and spawn.py (fire-and-forget spawn/status/result/wait/cancel/list, background=True). `subagent(action="wait")` is the fleet next-completed primitive (registry.wait; spawn N → wait → replace → wait, no polling). The blocking path drives a durable+resumable child (delegate_runner.build_resumable_child_runner/drive_child) that RESURFACES risky-op approvals to the user (child_guard + resurface_context bubble budget); one approval per blocking call, then a written grant covers same-shape ops. A child can also spend that one bubble to escalate a free-text decision via `ask_parent` (ask_parent.py; bubbles the question the same way, the parent turn answers, the child resumes in place — the resume forwards the answer payload). Background subagent calls/routines stay headless (ask_is_deny; ask_parent returns a graceful no-parent result).
│   └── tools/                    # Core tools: file_ops, read.py (merged text+media read), artifacts, web_search, clarify, processes/ (bash + background); the `process` tool actions are list/poll/log/wait/wait_for/kill/write — `wait_for(pattern=<regex>)` blocks server-side until a bg process's output matches (readiness/failure wait) instead of poll-looping. The `routine` authoring tool is in horizon/routines/tools.py.
├── web/                          # Vite 8 + TanStack Router/Query + React 18 + Tailwind 3 + @a2a-js/sdk
├── terraform/                    # Cloud Run + Cloud SQL + Cloud Scheduler + IAM + skills bucket
├── scripts/
└── tests/
    ├── unit/                     # Deterministic pytest
    ├── integration/              # InMemorySessionService + InMemoryMemoryService end-to-end
    ├── smoke/                    # Gated by RUN_SMOKE=1 (RUN_SMOKE_LLM=1 for LLM-hitting)
    ├── eval/datasets/            # Agent Platform datasets — `agents-cli eval`
    └── eval/evalsets/            # ADK evalsets — `adk eval`, replay suites only
```

**Agent / serving split (dependency points one way).** `agent.py` is the agent — owns construction (`_build_app_object`: tools, ordered callback chains, plugins, `App(...)`, module state) and the `app`/`root_agent` singletons, built eagerly at import. `fast_api_app.py` serves it: `build_runner()` resolves session/memory/artifact services + the sandbox from the environment and returns an ADK `Runner` (embed without FastAPI — CLI/batch); `_build_app()` builds the FastAPI surface (A2A + all `/lha/*` + `/scheduler/*` routers, mounted unconditionally) exposed as the lazy `horizon.fast_api_app:app` via module `__getattr__`. `import horizon` stays offline because the package `__init__` only exposes `__version__` (doesn't import `agent`); reading `horizon.agent` / `horizon.fast_api_app.app` triggers the eager `app = _build_app_object()`. **No factory/DI layer** — configure by environment (`LHA_ROOT_MODEL`, `LHA_ENVIRONMENT_BACKEND`, service URIs), install a custom `Environment` via `set_environment_provider` (`environment_context.py`), mount your own routes via `app.include_router(...)`, and adapt anything deeper by editing `agent.py`. `agent.py` sets `GOOGLE_CLOUD_LOCATION`/`GOOGLE_GENAI_USE_VERTEXAI` via import-time `setdefault` (an already-exported value wins) and probes ADC for the project only when `GOOGLE_CLOUD_PROJECT` is unset. Per-layer model: `docs/security-model.md`. Reader-facing guides: `docs/quickstart.md`, `docs/extending.md`, `docs/configuration.md`.

## ADK Callback Wiring

`agent.py` registers callbacks as **ordered lists** — order is the contract. Insert new callbacks at the correct point.

| Stage | Chain (top-to-bottom = run order) |
|---|---|
| `before_agent_callback` | `on_session_start_callback` → `bind_session_skills_callback` |
| `before_model_callback` | `select_model_callback` → `prune_tool_outputs_callback` → `redact_artifact_urls_callback` → `_slash_command_dispatcher` → `system_prompt_assembly_callback` → `reminder_injection_callback` → `subagent_description_callback` (halt short-circuit lives in `GuardrailsPlugin`, runs first via plugin layer) |
| `after_model_callback` | (empty on agent — no-progress detection in `GuardrailsPlugin.after_model_callback`) |
| `before_tool_callback` | `before_tool_log_callback` → `exfil_guard` → `policies_guard` (Layer C; consults `command_safety.classify()` + seed/overlay; headless child chains — background `subagent`/routine — pass `ask_is_deny=True` so "ask" verdicts hard-block, but a **blocking `subagent` child resurfaces** the ask to the user) → `permission_guard` (Layer D interactive ask-layer, runs last; **shell tools default `allow`** — `_shell_decision` demotes to a prompt only on a `command_safety` "ask" verdict or command-substitution; the **`command_safety` demotion is skipped for an explicit grant/overlay allow** so "approve this session/always" sticks, while genuine command-substitution always prompts (the anti-obfuscation net a grant can't buy off). Substitution detection is **quote-aware** — a backtick-quoted BigQuery table ref inside `'…'` expands nothing and is not substitution; ANSI-C `$'…'` processes escapes (`$'\''` is one literal quote) and unbalanced quoting fails closed. Auto-derived `commandPrefix` skips self-contained `--flag=value` tokens between binary and subcommand (`gcloud --project=p compute instances delete` → `gcloud compute`, not the whole binary) and matches flag-tolerantly when the prefix names no flag, so one approval covers any flag ordering; a **bare** flag stops the scan (`kubectl -n default delete` → `kubectl`) since it may consume the next token as its value, and `sudo` is never unwrapped (`sudo python3 -c …` → `sudo python3`) so a plain grant can't cover the escalated form — derivation and matching share one token walk (`command_classify.significant_tokens`) so they can't disagree; the seed opens `memory`/`reload` and keeps `routine` + other non-shell tools on the `*: ask_user` fallback; YOLO mode auto-approves; blocking and background `subagent` calls exempt via `SUBAGENT_TOOLS` — spawning grants the child nothing its own guard chain doesn't already gate, so the spawn-time prompt is friction) |
| `after_tool_callback` | `skill_telemetry_callback` → `tool_call_log_callback` — repeated-failure halt in `GuardrailsPlugin.after_tool_callback` |
| `after_agent_callback` | `auto_capture_callback` → `skill_curator_callback` → `review_fork_callback` (post-turn memory sync, skill promote/demote, judge fork — all throttled) |

**Per-turn steering rides the message tail, not the cached prefix:** `system_prompt_assembly_callback` appends only stable + context tiers to `system_instruction`; `reminder_injection_callback` (`horizon/conversation/reminders.py`) appends the volatile tier (iteration/last_error/date), near-budget warning, and last-error nudge as trailing `<system-reminder>` `Content` on `llm_request.contents` — keeping the prefix byte-stable for the cache. Budget/guardrail halts run a one-shot **graceful handoff** on the first halted turn (`horizon/conversation/graceful_halt.py` strips tools + injects a text-only handoff reminder, returns `None`) so the model produces a final summary, then fall back to the bare `[halted: ...]` envelope. The reminder callback no-ops on a handoff turn so the handoff reminder stays last.

**Plugins** (run before agent-level callbacks at each hook): `App(plugins=[IterationBudgetPlugin(), SIBLING_AGENT_PLUGIN, GuardrailsPlugin()], context_cache_config=..., resumability_config=ResumabilityConfig(is_resumable=True))`. `GuardrailsPlugin` consolidates `halt_consumer_callback` (before_model), `NoProgressGuard` (after_model), `RepeatedFailureGuard` (after_tool) — three guards share `session.state["halt_reason"]`, reset via `clear_halt_state` at turn boundary.

**Compaction:** `App(events_compaction_config=EventsCompactionConfig(summarizer=HorizonSummarizer(...), token_threshold=750_000, event_retention_size=20, compaction_interval=8, overlap_size=2))`. `HorizonSummarizer` subclasses ADK's `LlmEventSummarizer` to (a) prepend the REFERENCE-ONLY banner and (b) fire `spawn_flush_fork` against soon-to-be-compacted events. Reads memory-service handles from `compaction_context.py`'s ContextVar (set in `on_session_start_callback`).

| Horizon feature | ADK mechanism |
|---|---|
| `on_session_start` | `before_agent_callback` (first invocation only, state-key guarded) |
| Memory prefetch | `HorizonPreloadMemoryTool()` in root agent's `tools` (`horizon/memory/preload.py`) — calls `memory_service.search_memory()`, injects capped hits into the **contents**, not `system_instruction` |
| Post-turn memory sync | `auto_capture_callback` (after_agent) — NOT a blanket `add_session_to_memory()` |
| Tool block | `before_tool_callback` returns `{"error": ...}` |
| Output transform | `after_model_callback` returns modified `LlmResponse` |
| Halt | Set `session.state["halt_reason"]` → `GuardrailsPlugin.before_model_callback` consumes next turn |

## Web / UI

Vite 8 + TanStack Router + TanStack Query 5 + React 18.3.1 + TypeScript 5.6 + Tailwind 3.4 + `@a2a-js/sdk`. (Off Next.js + SWR.)

- **Build**: `npm run build` = `vite build` → static SPA in `web/dist/` (`web/index.html` entry, `web/src/main.tsx` mounts router). Code-split per route.
- **Production**: `web/dist/` served by the `web/server/` Express proxy (`STATIC_DIR = web/dist`), which gates everything behind IAP, SPA-falls-back to `dist/index.html`, reverse-proxies `/lha`, `/a2a`, `/feedback`, `/tick`, `/dream-review`, `/.well-known/*` to the backend Cloud Run service with a minted ID token, and **forwards the verified IAP JWT as `X-LHA-IAP-Assertion`** (no plaintext `X-LHA-User-Id`). FastAPI does **not** serve the web bundle.
- **Dev**: `npm run dev` = `vite --port 3000`; the Vite proxy forwards the same paths → `${NEXT_PUBLIC_LHA_URL}` (default `http://127.0.0.1:8001`), SSE unbuffered. (`NEXT_PUBLIC_*` names retained for continuity.)
- **Routing**: file-based routes in `web/src/routes/`; `@tanstack/router-plugin` regenerates `routeTree.gen.ts`. Both `/` and `/c` carry an optional typed `?id=` search param (not a dynamic segment) and render shared `web/components/chat/chat-route.tsx` (`ChatShell`). New chat starts at `/`; **first send locks contextId by adding `?id=` on the current route** (search-only change → `ChatShell` not remounted mid-stream). A pathname change (`/`→`/c`) remounts and aborts the live A2A stream, so it's avoided. `web/app/` holds only shared assets (`providers.tsx`, `globals.css`).
- A2A client posts to same-origin `/a2a` (agent card `rpc_url` rewritten client-side to relative).
- Data hooks use TanStack Query (`web/lib/query-client.ts`, `query-keys.ts`): `useLhaState`, `useLhaSessions`, `useLhaTasks`, `useLhaEvents`, etc.
- Brand color tokens: `lh.{weft, warp, thread, shuttle}` (`web/tailwind.config.ts` + `globals.css` `--lh-*`). Never reintroduce `recipe-*`.
- Components: `web/components/{chat, panels, behind, brand, ui}/`.

## Sandbox + Environment Interface

Tools call the active **`Environment`** (`horizon/environment/base.py`) through the `environment_context.py` ContextVar, never the host directly. `Environment` is Horizon's contract — a superset of ADK's `BaseEnvironment` that adds `list_directory`/`delete_file`/`make_dir`/`download_zip`/`upload_zip`/`spawn_process` plus capability flags (`on_host_fs`, `cache_identity()`). Two backends implement it: **`local`** (`horizon/environment/local.py:LocalEnvironment`, default for tests) and **`sandbox`** (`horizon/environment/sandbox.py:SandboxEnvironment`, a BYOC Sandbox, `LHA_ENVIRONMENT_BACKEND=sandbox`, runs in the `horizon/sandbox/runtime/` shim). Callers dispatch by method or capability flag — **never `isinstance` on a concrete env class** (zero remain) — so a third backend (installed via `set_environment_provider(factory)`) routes correctly instead of falling into the host-fs path. Per-turn auth is **env-owned**: `Environment.refresh_auth() -> bool` (default no-op `True`; SandboxEnvironment self-refreshes via injected minter closures, returns `False` when the backend is gone), so `session_start._refresh_sandbox_auth` calls `env.refresh_auth()` polymorphically and the light `set_environment_provider` hook gets refresh too. Always go through the interface — never `subprocess.run` or open files directly in tools. Sandbox egress is **one env var, no in-session toggle**: `provider.default_internet_access()` = `env_flag("LHA_SANDBOX_INTERNET_ACCESS", default=not _in_prod())`, i.e. **hermetic when deployed** (Cloud Run sets `K_SERVICE`) and **internet-on in local dev** so `make dev` works out of the box; an explicit value wins either way. Hermetic means provisioned from an internet-off Vertex template (kernel/network-level, not a heuristic). Vertex bakes egress in at create time, so a change applies on the **next provision** — there is no live toggle, and `egress_control_config` is binary `internet_access` (no host allowlist). The mode threads through provisioning: `lifecycle.ensure_template(internet_access=)` selects a per-mode template `display_name` (`template_display_name`) and stamps `egress_control_config.internet_access`. A reattached env recovers its mode by reading the sandbox's `sandbox_environment_template` → template `egress_control_config.internet_access` (`lifecycle.template_internet_access` via `provider._sandbox_internet_access` / `_template_internet_access`; display-name suffix fallback) — NOT a `description` stamp (the sandbox create API rejects a `description` field). `SandboxEnvironment.internet_access` exposes it, and `/sandbox-upgrade` preserves the prior sandbox's mode rather than resetting to the default. The same default applies to both the **interactive user sandbox** and **routine** sandboxes (isolated `lhart-*`, secret-scoped): routines are unattended so they inherit it — one knob, no per-surface override. Layer A (exfil_guard) remains the exfiltration boundary (secret material, credential reads, metadata-server access, and upload-shaped commands to non-allowlisted hosts) regardless of egress mode.

**Provisioning interface (`SandboxProvider`, `horizon/sandbox/provider.py`).** Per-user provisioning/reattach/snapshot/upgrade live behind a `SandboxProvider` protocol (per-turn auth refresh is env-owned, not a provider method) — `VertexSandboxProvider` (composes `sandbox/lifecycle.py`) or `LocalProvider`, selected by `LHA_ENVIRONMENT_BACKEND` or overridden with `set_sandbox_provider(provider)` (`environment_context.py`). `session_start.py` owns only the backend-agnostic orchestration (the per-`user_id` `_env_cache`, `_provisioning_locks`, ContextVar binding, `_finalize_env` = initialize→migrate→egress-push, atexit, and 404-eviction) and delegates every Vertex-specific step to the active provider (no concrete-class `isinstance` remains — `_refresh_sandbox_auth` calls `env.refresh_auth()`, which returns `False` to signal a gone sandbox so the orchestrator evicts). An external backend registers its own provider for full lifecycle parity, or uses the lighter `set_environment_provider(user_id) -> Environment` hook to bring just an env and skip provisioning.

**Custom Python**: users write `scripts/<name>.py` in their workspace and run it via `bash`. No extension/plugin discovery path; `bash`/`process` are the integration surface.

## Scheduler (dream-review, routines, snapshot)

Cloud Scheduler integration. Routers read the persistent session_service + memory_service + agent via `app.state.runner`, keyed on the runner's `app_name`.

- **dream-review**: nightly `/scheduler/dream-review` with empty `user_ids` auto-discovers every user active in the lookback window (`dream_review.list_active_users`), then per user runs `memories.generate` over recent events to consolidate a native Structured Profile.
- **snapshot**: daily `/scheduler/snapshot` (`snapshot_endpoint.py`) per-user sandbox snapshot+prune (Phase C, TTL survival); no-ops unless `LHA_SNAPSHOT_ENABLED`.
- **Routines** (unattended recurring tasks, run through the **shared A2A handler** — `app.state.a2a_handler`, same `DefaultRequestHandler` the web uses — targeting a pre-tagged session by `context_id`, which records an A2A Task + `LHA_TASK_IDS_KEY` like a normal chat; driving the runner directly would leave the UI blank): `routine_store.py` (RoutineRow + RoutineStore protocol + InMemoryRoutineStore + `get_routine_store` gated by `LHA_ROUTINE_STORE`), `routine_postgres_store.py` (asyncpg, reuses `LHA_REMINDER_DB_URL`), `cron.py` (croniter helpers), `routine_tick_endpoint.py` (POST `/scheduler/routine-tick` → claim_due → `_fire_routine` → `_run_routine_turn`). The fire turn is wrapped in three ContextVars (set_routine_run / set_routine_secret_scope / set_headless_mode) so it runs in a fresh isolated `lhart-<id>` sandbox, with only the routine's declared secrets; shell runs unattended in that sandbox while non-shell approvals are denied. Doc: docs/routines.md. A created routine has no tagged session until it first fires, so the web surfaces upcoming routines via `GET /lha/routines` (`horizon/api/routines.py`, read-only over the RoutineStore) in the side-panel **Scheduled** section via `web/components/panels/scheduled-panel.tsx` (thin wrapper over `routines-panel.tsx`; the reminder capability it used to also render was removed). The right rail also has a live **Background** section (`web/components/panels/background-panel.tsx` + `useProcesses` hybrid-polling hook) listing the user's running sandbox processes with a per-process kill, backed by `GET`/`DELETE /lha/processes` (`horizon/api/processes.py`) over the `BaseEnvironment.list_processes/kill_process` interface.

## Database connection resilience

All four SQL paths — ADK web-path session service, lifespan Runner session service, A2A `DatabaseTaskStore`, raw-asyncpg routine pool (`scheduler/routine_postgres_store.py`) — go through `horizon/infrastructure/db_resilience.py`. SQLAlchemy engines get `resilient_engine_kwargs()` (`pool_pre_ping` + `pool_recycle` + `connect_args.timeout`); the asyncpg pool gets `max_inactive_connection_lifetime` + `timeout`. **`pool_pre_ping` only validates at checkout** — it does NOT cover a connection severed mid-query (Cloud SQL failover) or a refused connect during a proxy bounce. That gap is why every op is also wrapped in `retry_on_disconnect()` (3 attempts, exp backoff, retries only `is_transient_disconnect()`). `ResilientSessionService` + `_ResilientTaskStore` (in `fast_api_app.py`) are the wrappers, applied only on pooled URLs (`is_pooled_sql_url` — **Postgres only**; `mysql` is deliberately excluded since it has no disconnect-type coverage). **`asyncpg` is imported lazily** (`_transient_disconnect_types()`, a `postgres`-extra) so a core-only install can still `import horizon.fast_api_app` and call `build_runner()`. Probe: `tests/smoke/backend/test_db_resilience_smoke.py` (gated `RUN_DB_RESILIENCE_PROBE=1` + `LHA_PROBE_DB_URL`).

## Testing

```bash
uv run pytest tests/unit tests/integration              # deterministic — default
RUN_SMOKE=1 uv run pytest tests/smoke                   # hits FastAPI, no LLM
RUN_SMOKE=1 RUN_SMOKE_LLM=1 uv run pytest tests/smoke   # adds LLM-hitting smokes
# Probes are `probe_*.py`, which pytest's default `python_files` does NOT collect
# from a directory argument — name the file explicitly.
RUN_SANDBOX_PROBE=1 uv run pytest tests/integration/probes/probe_cuj1_ephemeral.py
RUN_CUJ_PROBE=1 uv run pytest tests/integration/cuj_probes/probe_cuj13_scheduler_auth.py
agents-cli eval run --dataset tests/eval/datasets/<name>-dataset.json \
  --config tests/eval/eval_config.yaml                  # LLM behavior, 69 cases
uv run python scripts/eval_gate.py                      # applies the 0.8 threshold
uv sync --extra eval                                    # one-time, for the 2 suites below
uv run adk eval tests/eval/horizon_eval \
  tests/eval/evalsets/<name>.evalset.json \
  --config_file_path tests/eval/eval_config.json        # memory_recall, daily_news_bot_journey
agents-cli lint --fix                                   # ruff + ty + codespell
```

Autouse fixtures (`tests/conftest.py`): `_hermetic_environment` (strips host env), `_scoped_environment` (per-test `LocalEnvironment` in the ContextVar), `runner_factory` (Runners with `InMemorySessionService`/`InMemoryMemoryService`).

**Two eval runners, split by whether anything before the graded turn had to *run*.** 69 cases live in `tests/eval/datasets/*-dataset.json` (Agent Platform format) and run through `agents-cli eval`; each carries its own rubrics in `rubric_groups.horizon`, which `tests/eval/eval_config.yaml` selects via `rubric_group_key`. Grading reports scores, so `scripts/eval_gate.py` applies the 0.8 pass threshold. Earlier turns ride along as seeded context rather than being replayed, which suits a case whose earlier turn *is* context — `compression_quality`'s first turn is a pasted compaction banner, so it migrated. Nine cases stay on `adk eval` (with `tests/eval/eval_config.json` and the `tests/eval/horizon_eval/` shim, needed because `adk eval` resolves `<pkg>.agent.root_agent` and `horizon/__init__.py` deliberately does not import `agent`): `memory_recall` (8), where recall is only meaningful across a session boundary and an eval case is one session, and `daily_news_bot_journey` (1), whose first turn creates a routine row the second edits.

Seven dataset cases pre-set `session.state` through a leading `state_delta` event, which needs a current `agents-cli`; on an older one the state is not applied.

Unit tests do **not** need GCP — only the two eval runners, `agents-cli run`/`playground`, and `make dev` hit Vertex.

## Dev Loop

```bash
make dev                # fresh-clone: bootstraps deps + .env, then backend (:8001) + web (:3000)
make dev-local          # forces LHA_ENVIRONMENT_BACKEND=local, ignores .env
make dev-sandbox        # forces LHA_ENVIRONMENT_BACKEND=sandbox, ignores .env
make dev-backend        # backend only
make dev-web            # web only
make dev-stop           # kill whatever is listening on :8001 / :3000
make deploy             # deploy-backend + deploy-web (Cloud Run)
agents-cli run "prompt" # one-shot smoke test
agents-cli playground   # interactive browser UI
```

`make dev` is fresh-clone friendly (file-target prereqs `.env`/`.uv-installed`/`web/node_modules` install on first run, no-op after; seeds `.env` from `.env.example`). Backend default is **`local`** (`LHA_ENVIRONMENT_BACKEND ?= local`); an explicit value in `.env` wins, `make dev-sandbox` forces sandbox. Empty `GOOGLE_CLOUD_PROJECT` falls back to active `gcloud config`. Backend port **8001**. Vite dev proxy reads `NEXT_PUBLIC_LHA_URL`.

The dev stack is **fail-fast and single-instance**: `dev` depends on `dev-preflight`, which refuses to start when :8001/:3000 are already bound (`make dev-stop` frees them), each half `kill 0`s the process group when it exits so a dead backend takes the web server with it, and `dev-web` passes `--strictPort` so vite can't silently drift off the port the backend's `ALLOW_ORIGINS` is pinned to. `dev-backend`/`-local`/`-sandbox` are one recipe; the variants only set the target-specific `DEV_BACKEND_ENV`.

## Sandbox runtime image

When `horizon/sandbox/runtime/` changes (Dockerfile, server.py, protocol.py, entrypoint.sh), rebuild via **Cloud Build** — never local `docker build`:

```bash
# One-time: nothing in terraform/ creates the `lha-sandbox` repo, and the APIs
# below are only enabled by Terraform at `make deploy` — too late for this build.
gcloud services enable artifactregistry.googleapis.com cloudbuild.googleapis.com --project=$PROJECT_ID
gcloud artifacts repositories create lha-sandbox \
  --repository-format=docker --location=us-central1 --project=$PROJECT_ID

gcloud builds submit horizon/sandbox/runtime \
  --tag=us-central1-docker.pkg.dev/$PROJECT_ID/lha-sandbox/runtime:vX.Y.Z \
  --project=$PROJECT_ID
```

Then bump `LHA_RUNTIME_IMAGE` in `.env` + the Cloud Run env var (Terraform `lha_runtime_image`). Version bumps: patch=fixes, minor=new endpoints, major=protocol breaks. Tag list: `gcloud artifacts docker tags list .../lha-sandbox/runtime`.

Sandbox identity is **version-scoped** (`display_name = lha-<user>-<tag>`, `horizon/sandbox/lifecycle.py`) but **reattach is version-agnostic**: `find_latest_user_sandbox` reattaches to the most-recent RUNNING sandbox regardless of version, so a rollout never wipes installed CLIs (they live in `$HOME`/`~/.local`, outside the migrated `/workspace`). Upgrades are **explicit**: **`/sandbox-upgrade`** provisions the current image, migrates `/workspace` (zip → `POST /files/zip`), deletes the prior, hot-swaps. Set **`LHA_RUNTIME_MIN_VERSION`** (off by default) to force-upgrade sandboxes below a floor on next session — the escape hatch for breaking shim/protocol changes.

TTL survival (Phase C, **off by default**): with **`LHA_SNAPSHOT_ENABLED`**, the daily `/scheduler/snapshot` job snapshots each active user's full `$HOME` and prunes to `LHA_SNAPSHOT_KEEP` (default 2); a session with no RUNNING sandbox restores from the latest snapshot before provisioning blank. A restored sandbox runs the snapshot's original image (consistent with "stay on your version until you `/sandbox-upgrade`").

## Environment Variables

> Reader-facing catalog: [`docs/configuration.md`](docs/configuration.md) (this section is the maintainer view).

`.env` is loaded by `make dev-backend` (`set -a; . ./.env; set +a`).

- `GOOGLE_CLOUD_PROJECT=<your-project-id>`, `GOOGLE_CLOUD_LOCATION=global`, `GOOGLE_GENAI_USE_VERTEXAI=True` — Vertex / Gemini / Memory Bank.
- `LHA_ENVIRONMENT_BACKEND` — `local` or `sandbox`.
- `LHA_AUTH_MODE` — request identity (`horizon/auth/identity.py:IdentityMiddleware`). `dev` (default) → no auth, every request is `LHA_DEV_USER_ID`; `iap` → verifies an IAP JWT against `LHA_IAP_AUDIENCE`, user_id = email; `trusted_header` → verbatim `X-LHA-User-Id` (trust via Cloud Run IAM). **Backstop:** `dev` refuses to run when `K_SERVICE` is set → `DevAuthInProductionError` (500). The deployed backend runs `iap`, accepting two shapes (no plaintext header): (a) the IAP JWT, forwarded by the `lha-web` proxy as **`X-LHA-IAP-Assertion`** (Google strips the native `X-Goog-IAP-JWT-Assertion` inbound to a non-IAP backend; both verified the same way); (b) on agent-to-agent calls, a **Gemini Enterprise** OAuth access token in `Authorization: Bearer`, verified via `tokeninfo` with `aud == LHA_GCP_OAUTH_CLIENT_ID` (`horizon/auth/oauth_verify.py`) and overlaid as `CLOUDSDK_AUTH_ACCESS_TOKEN` for the turn. GE ties this via `authorizationConfig.agentAuthorization`; the agent card needs no `securityScheme`. See `docs/security-model.md`.
- `LHA_DEV_USER_ID` — identity every request collapses to under `dev`. Default `dev@local`.
- `LHA_IAP_AUDIENCE` — required under `iap`; the Cloud Run audience `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`. Unset ⇒ 500.
- `ARTIFACT_SERVICE_URI` — explicit artifact-service URI override (symmetry with `SESSION_DB_URL`/`LHA_MEMORY_BANK_RESOURCE_NAME`); resolved by `fast_api_app._resolve_service_uris`. Takes precedence over `LOGS_BUCKET_NAME`; unset ⇒ `gs://{LOGS_BUCKET_NAME}` if set, else in-memory.
- `LOGS_BUCKET_NAME` — GCS bucket for the ADK artifact service (`GcsArtifactService`). Set ⇒ durable artifacts (survive restart, reliably loadable when the A2A FilePart is emitted); unset ⇒ in-memory (lost on restart). Created by `terraform/artifact_bucket.tf`. Artifacts stream back as A2A FileParts: the web UI renders them inline (images, audio) and shows **`text/html` as Open/Download links** — Open views it full-window in a no-JS `sandbox=""` iframe, Download serves the raw file for HTML that needs JavaScript. To show rich/visual output the agent writes a self-contained HTML file (static SVG/CSS, no JS) and `artifact(save)`s it.
- `LHA_WEB_URL` — **removed.** Artifact links no longer depend on the web frontend. `artifact(save)` returns a private GCS V4 **signed URL** (`horizon/tools/_artifact_links.py`) when `LOGS_BUCKET_NAME` is set — 7-day TTL, IAM SignBlob (no private key), per-user object path (`{app}/{user}/{session}/{filename}/{version}`), inline/attachment from the `inline` flag — so clients without our frontend (e.g. Gemini Enterprise) can open it; otherwise a same-origin `/lha/workspace/download` link (local dev). On the GE A2A path the backend lifts this `url` out of the `artifact` function_response into a `📎 [name](url)` text status message (`horizon/a2a/executor.py:_surface_artifact_links`), since GE buries data parts. The model never sees the signed URL: `redact_artifact_urls_callback` (before_model, `horizon/context/artifact_url_redaction.py`) replaces it with a placeholder in `llm_request.contents` (the deep copy the model actually reads — `session.events` keeps the URL for the converter + task history), so the model can't paste the credentialed blob into its reply (clients render the link via the FilePart / `📎` status). The `artifact` tool's own description tells the model the file is shown automatically and NOT to write its own link/URL/"here's your file" line (moved out of the system prompt into the tool description under the single-source-of-truth rule, prompt-minimalism Task 6); since the model otherwise invents a dead `attachment://<name>` link, `_strip_fake_artifact_links` (a2a converter) removes any `attachment:`/`sandbox:` links as a backstop. Inline HTML served from GCS lacks the endpoint's `Content-Security-Policy: sandbox` header but renders cross-origin-isolated on `storage.googleapis.com`; JS-bearing HTML should use `inline=False` (download).
- `LHA_ROOT_MODEL` — root agent default; a key in `horizon/models/registry.py` (e.g. `gemini-3.7-flash`). Default `gemini-3.7-flash`. `/model` overrides per-session.
- `LHA_VERTEX_SERVICE_TIER` — set to `priority` to pin Gemini to Vertex's `SERVICE_TIER_PRIORITY` per turn. Off by default (the tier needs a Vertex entitlement most projects lack); any other value runs on-demand.
- `LHA_SANDBOX_LOCATION` — Agent Platform region for sandbox provisioning. Default `us-central1`; independent of `GOOGLE_CLOUD_LOCATION`. Read at call time by `session_start.py:_sandbox_location`.
- `LHA_RUNTIME_MIN_VERSION` — sandbox upgrade floor (off by default). Set (e.g. `v0.12.0`) to force-upgrade sandboxes below the floor on reattach. Unparseable token/floor never forces an upgrade.
- `LHA_SNAPSHOT_ENABLED` — master switch for Phase C (snapshot TTL survival): gates the daily snapshot+prune job and restore-on-session-start. Off by default. `LHA_SNAPSHOT_TTL` (default `30d`), `LHA_SNAPSHOT_KEEP` (default `2`) tune retention.
- `LHA_GCP_OAUTH_CLIENT_ID` / `_CLIENT_SECRET` / `_REDIRECT_URI` — the **"Connect Google"** buttons (`horizon/auth/oauth.py`, `/lha/gcp/*`). Two connections via one Web OAuth client: **GCP** (`cloud-platform`) → `CLOUDSDK_AUTH_ACCESS_TOKEN` for `gcloud`/`bq`; **Workspace** (per-surface drive/gmail/calendar/… × read-only|read-write) → `GOOGLE_WORKSPACE_CLI_TOKEN` for `gws`. Each token is written as a per-user secret (so `secret_env()` forwards it into the sandbox). Access-token-only (no refresh, ~1h); the client secret is the HMAC key for signed `state`; callback cross-checks state's `user_id` against IAP identity. `_REDIRECT_URI` is the public `https://<web-host>/lha/gcp/callback`. Unset ⇒ `/connect` returns 503.
- `LHA_PRUNE_TOOL_OUTPUTS` — zeros old large tool-result bodies before the model reads them. On by default; `0` disables.
- `LHA_COMPACTION_WINDOW_FRACTION` — fraction (0,1) of the model's input window at which compaction fires. Default `0.75`; out-of-range falls back.
- `LHA_DREAM_REVIEW` — `0` disables dream-review (all paths return `{success: false}`). On by default.
- `LHA_DREAM_SESSION_LIMIT` — max recent sessions per user fed to profile consolidation. Default `50`.
- `LHA_DREAM_LOOKBACK_HOURS` — activity window for auto-discovering users in `/scheduler/dream-review`. Default `24`.
- `LHA_MEMORY_CONSOLIDATION` — `0` disables native consolidation of general memories during the dream pass (Memory Bank then adds without dedupe/reconciliation, `disable_consolidation=True`); the Structured Profile is unaffected. On by default; pass reports `consolidation: {ran, created, updated, deleted}`. Probe: `scripts/probes/probe_memory_consolidation.py`.
- `LHA_LIVE_MEMORY_TESTS` — `1` runs the live Memory Bank profile round-trip smoke (`test_memory_profiles_smoke.py`); off in CI.
- `LHA_ROUTINE_STORE` — routine schedule-row backend: `memory` (default, `InMemoryRoutineStore`, not durable) or `postgres` (`PostgresRoutineStore`, reuses `LHA_REMINDER_DB_URL`; unset under `postgres` raises at startup). Selected by `scheduler/routine_store.py:get_routine_store`.
- `LHA_SECRET_BACKEND` — per-user secret store backend: `secretmanager`/`gcp` (default, `SecretManagerStore`, needs `GOOGLE_CLOUD_PROJECT`; `google.cloud.secretmanager` imported lazily only in this branch) or `memory` (`InMemorySecretStore`, not durable). Selected by `secrets/store.py:get_secret_store`; `set_secret_store()` installs a custom `SecretStore`. The GCP `NotFound`/`AlreadyExists` translation lives inside `SecretManagerStore` so a non-GCP store needs zero `google.api_core` imports.
- Old `RECIPE_*` prefixes in `.env` → rename to `LHA_*`; the code only reads `LHA_*`.

## Models

- Root agent: `DispatchingLlm` (`horizon/models/`) routes each turn to the backend named in `llm_request.model`. `_MODELS` (`registry.py`) holds the Gemini backends — `gemini-3.7-flash` (default) + `gemini-3.1-pro-preview`, both selectable via `/model`. Gemini is the only supported provider; the registry keeps the interface so adding another (e.g. a `LiteLlm` entry) stays a one-line change. `MODEL_REGISTRY` is a lazy `Mapping` (backends build on first access, so `import horizon` needs no GCP creds). Gated behind `LHA_VERTEX_SERVICE_TIER=priority` (off by default), the gemini entry is a `_PriorityGemini` subclass that pins Vertex's `service_tier=SERVICE_TIER_PRIORITY` per turn (google-genai's `ServiceTier` enum only carries the Developer-API spelling, so the Vertex literal is fed raw via the enum's unknown-value fallback; guarded so an SDK change degrades to on-demand rather than breaking registry import). Default from `LHA_ROOT_MODEL`; per-session override via `/model <name>` (writes `selected_model`); `select_model_callback` stamps `llm_request.model` each turn. **Per-backend capability interface (no `isinstance`/name-gating):** each model in `registry.py`'s single `_MODELS: dict[str, ModelDescriptor]` table (builder + `input_token_limit` + `capabilities`) carries a `ModelCapabilities` (`horizon/models/capabilities.py`: `max_image_bytes`, `can_view_mime`, an optional `prepare_contents` content hook). `DispatchingLlm` runs `prepare_contents` when a model sets it (Gemini = passthrough); `read`'s media path reads the same capabilities up front (`caps.can_view_mime` / `caps.max_image_bytes`) to refuse an unviewable or oversized inline image before a wasted round-trip. **Add a model = one `_MODELS` entry** (a model with unusual media limits or content quirks sets its own capabilities) — no `dispatcher.py`/`read.py` edit, no new `isinstance`.
- `web_research` subagent + compaction summarizer (`HorizonSummarizer`): `gemini-3.7-flash` — pinned.
- `subagent` children accept the same models as the root: `delegate_builder._ALLOWED_MODELS` is `frozenset(MODEL_REGISTRY)`, derived rather than duplicated, so a new `_MODELS` entry is selectable by children too.

Don't change registry defaults or subagent models without being asked. `/model` and `LHA_ROOT_MODEL` are the user-facing knobs.

## State keys (session.state)

Load-bearing keys read across callbacks — if you rename, update every reader:
- `halt_reason` — set in `after_tool_callback`, consumed by `halt_consumer_callback` next turn.
- `selected_model` — set by `/model`, consumed by `select_model_callback`.
- `approval_mode` — `"default"` | `"yolo"`, toggled by `/yolo`. YOLO auto-approves the Layer-D interactive ask (demotable risky ops like `git push --force`, `find -delete`); does NOT bypass exfil/Layer-C deny. Read by `permission_guard`. Per-session, not persisted.
- `permission_grants` — per-session `list[dict]` of permission rules (gemini shape) written by `permission_guard` on a "this session" (`proceed_always`) approval; read by `permission_guard` + `/permissions`. SEPARATE from the security `_policy_grants` store used by `/grant` (different layer — soft ask-layer, not hard-deny bypass).
- Skill bind state — written by `bind_session_skills_callback`.
- Iteration counter — owned by `IterationBudgetPlugin`.
- `system:session_source` — `"scheduler"` on sessions from a scheduled job (absent ⇒ normal chat). Read by `api/sessions.py` to filter `/lha/sessions?source=`.
- `system:scheduler_job` — `"dream_review"`|`"routine"`; written by `scheduler/sessions.py:create_scheduled_session`.
- `workspace_window` — per-session `list[str]` of workspace-relative dirs the session is anchored to. Written by `/workspace` (auto-seeded on first subdir write, no agent-callable tool); read by path tools via `workspace_window.py`'s `resolve_in_window`, rendered as the `Focus:` line. **A lens, not a security boundary** — `horizon/tools/_paths.py:path_under_root` is the only trust gate and runs last, so any in-root path still reaches the whole workspace; `..` stays blocked. Absent/empty ⇒ whole workspace.

### Routine-run ContextVars (async-local, not session.state)

The routine fire path (`routine_tick_endpoint.py:_fire_routine`) wraps the turn in three `ContextVar`s, all reset in `finally` so concurrent web turns are untouched:
- **routine-run** (`routines/run_context.py`: `set_routine_run`/`active_routine_run`/`reset_routine_run`, holding `RoutineRun(routine_id, owner)`) — read by `_ensure_environment`, which keys the env cache on `("routine", routine_id)` and builds a fresh isolated `lhart-<id>` sandbox (`find_routine_sandbox`/`routine_display_name`; `lhart-` is disjoint from the user's `lha-<user>-`, no migration/upgrade/snapshot).
- **routine secret-scope** (`secrets/inject.py`) — `secret_env()` filters resolved secrets to the routine's declared names; the declared `secrets` list IS the blast-radius boundary.
- **headless mode** (`guardrails/permission_guard.py`) — no user to prompt, so an `ask_user` on a shell command (bash/process write) is allowed (it runs in the isolated `lhart-` sandbox) while a non-shell `ask_user` becomes a deny (`headless_denied`); exfil/destructive guards run earlier and still hard-block regardless.

## Workspace conventions

- `lha/tool-output/` — overflow spill for oversized tool output (bash overflow).
- `lha/config/` — durable home for **credentials/config** of on-demand CLIs (gws, gcloud, mcp-cli). Persistence boundary: `$HOME` persists but a runtime-image upgrade re-provisions and migrates **only `/workspace`** (zip via `/files/zip`, which drops symlinks + strips exec bits, so it carries data not binaries). So **binaries → `~/.local`** (cheap to reinstall via the skill's `command -v` check) and only credentials go under `lha/config/`. The image puts `~/.local/bin` on `PATH` (the one tool-agnostic fact it owns); tool-specific config-dir env vars (`GOOGLE_WORKSPACE_CLI_CONFIG_DIR`, `CLOUDSDK_CONFIG`, `MCP_CONFIG_PATH`) point at `lha/config/` and live in the `bootstrap-google-tools` skill, not the image.
- `.lha/` (dotted) — per-user **overlay** for tenant config the agent must NOT self-edit: `exfil.jsonl` (egress `allow_hosts` + exfil rules), `policies.jsonl` (tool policies), `permissions.jsonl` (persisted permission rules, written by the backend on an "always allow"), `routines/<id>.yaml` (human-readable routine manifests, written by the backend on a confirmed `routine(action="create")` — the runtime copy the fire path reads is the RoutineStore row, NOT this file). The agent itself cannot write `.lha/`.
- **Workspace window (per-session project focus).** When set, `search_files`/`read`/`write`/`edit` resolve default `.`/relative names under the window's first dir (`workspace_window.py`); break out with a leading-`/` path or, for `search_files`, `scope="workspace"`. User sets/clears with `/workspace <dir>` / `/workspace /` (no agent-callable tool — `set_workspace_window` was dropped as agent-facing ceremony); first write into a top-level subdir auto-seeds. `bash` cwd stays **global by default** (the explicit break-out surface). A default + lens, never a security boundary.

## Don'ts

- Don't `pip install` — `uv add <pkg>` then `agents-cli install`.
- Don't add Vertex AI calls in unit tests — use `InMemoryMemoryService`/`InMemorySessionService`.
- Don't change model registry defaults or subagent models without being asked.
- Don't add tools depending on out-of-scope services (browser, image gen, etc.).
- Don't write multi-line docstrings or "what this does" comments. Single-line WHY-only where the why is non-obvious.
- Don't write comments rationalizing a refactor ("re-imported here because…", "kept for backwards compat…") — that's commit-message material.
- Don't `subprocess.run` or touch the filesystem directly from tools — go through `BaseEnvironment`.
- Don't reintroduce Next.js or SWR. The UI is a Vite SPA, same-origin backend calls, served in prod by `web/server/` and in dev by the Vite proxy.
- Don't reintroduce `recipe-*` color tokens or `RECIPE_*` env vars. Canonical: `LHA_*` / `lh-*`.
- Don't pass kwargs to `PreloadMemoryTool()` — installed ADK's `__init__` is `(self)` only.
- Don't put per-turn content in `system_instruction`. `GeminiContextCacheManager._generate_cache_fingerprint` hashes the whole string, so anything that varies turn to turn invalidates the context cache for the entire prefix. A memory block rebuilt every turn from a similarity search is the worst case: with memories present the cache never validates and the whole static prefix is re-billed at full price. ADK places the block via `LlmRequest._insert_transient_user_content`, which `_find_count_of_contents_to_cache` excludes by design; `HorizonPreloadMemoryTool` (`horizon/memory/preload.py`) subclasses the stock `PreloadMemoryTool` only to cap it with `LHA_PRELOAD_MAX_MEMORIES` / `LHA_PRELOAD_MAX_CHARS`, because `BaseMemoryService.search_memory` has no `top_k`.

## Operational Guidelines

- **Code preservation**: only modify code the request targets. Preserve surrounding code, config, comments, formatting.
- **Model 404 errors**: fix `GOOGLE_CLOUD_LOCATION` (e.g. `global`), not the model name.
- **ADK tool imports**: import the tool instance, not the module (`from google.adk.tools.load_web_page import load_web_page`).
- **Run Python with `uv`**: `uv run python script.py`.
- **Stop on repeated errors**: same error 3+ times → fix the root cause, don't retry.
- **PRs / git host**: use standard `git` + the GitHub CLI (`gh pr create --base main --title ... --body-file ...`, `gh pr view`) against the public remote.

## Pinned versions

- `google-adk[mcp,otel-gcp,a2a]>=2.8.0,<3.0.0` (ADK **2.8.x** — ships the a2a 0.3↔1.x `_compat` bridge).
- `a2a-sdk[http-server,sqlite]>=1.0,<2` (a2a **1.x**; proto types, `[http-server]` routes + `[sqlite]` `DatabaseTaskStore`. The v0.3 compat layer keeps Gemini Enterprise working). Web `@a2a-js/sdk` **1.0.1** — the web UI speaks the native 1.0 wire; Gemini Enterprise uses the v0.3 interface, served from the same `/a2a` endpoint.
- `croniter>=6.2.2` — 5-field cron for routine schedules (`scheduler/cron.py`).
- Lint stack: `ruff` + `ty` (Astral's Rust type checker, replacing mypy) + `codespell`.
- Optional extras (`pyproject.toml`): `lha[postgres]`, `lha[scheduler]`, `lha[data]`, `lha[full]`/`lha[all]`. Core install is Gemini-only; dev/test pulls `lha[full]` via the `dev` dependency-group. Catalog: `docs/configuration.md`.
