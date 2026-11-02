# ADK Voice Agent: guide for agents and developers

1. **[Studying the recipe](#studying-the-recipe)**: how ADK Voice Agent keeps a Gemini Live voice agent responsive while coding agents work on each user's computer.
2. **[Maintaining the code](#maintaining-the-code)**: rules, repo layout, and commands when changing this repo.

---

# Studying the recipe

## The problem

Voice is synchronous and short-lived; coding work is slow and stateful. Combined naively:

- Blocking tool calls stall the audio stream.
- Reading a 60-line diff aloud is useless.
- Work in one thread is lost to the next without a persistent disk and named project folders.
- Coding agents holding OAuth tokens can send mail without asking.

## Recipe table

| # | Pattern | Start here | Scope | Deep dive |
|---|---|---|---|---|
| 1 | **Non-blocking voice tools.** Slow tools return a pending status at once, finish in the background, and deliver results with `WHEN_IDLE`. A quick lookup over 8 s yields one "still working" line. | [`app/voice/non_blocking.py`](app/voice/non_blocking.py) → `non_blocking_tool()`, `thread_tool()` | Any `run_live` agent | [Architecture: a voice turn](docs/architecture.md#a-voice-turn) |
| 2 | **Exactly-once milestones.** Each thread milestone is announced once, to the waiting tool call or as a background update. | [`app/voice/thread_updates.py`](app/voice/thread_updates.py) → `ThreadUpdates` | Transferable | [Architecture: a voice turn](docs/architecture.md#a-voice-turn) |
| 3 | **A personal computer per user.** The daemon serves `/exec`, `/runs/*`, `/files/*`, and `/activity` behind `Environment`. `ComputerProvider` owns lifecycle (host dir, Docker, static URL, or GCE VM + disk). Idle computers stop; voice sessions pre-warm; warm hours keep them on during workdays. | [`app/workers/providers/base.py`](app/workers/providers/base.py) → `ComputerProvider` ([`registry.py`](app/workers/providers/registry.py)), [`reaper.py`](app/workers/providers/reaper.py) → `reap_once()`, `warm_once()`, [`app/workers/environment.py`](app/workers/environment.py) → `Environment` | Any agent needing a Linux box | [Architecture: where code runs](docs/architecture.md#where-code-runs), [Extending: add a computer provider](docs/extending.md#add-a-computer-provider) |
| 4 | **The home directory as memory.** Threads work in git repos under `code/<repo>`, `projects/<slug>`, or `scratch/<date>-<slug>`. The first writer leases the folder; a concurrent writer gets a git worktree merged back on finish. The server commits once per round and reads diffs via `git diff`. | [`app/workers/workspace.py`](app/workers/workspace.py) → `resolve_project()`, `open_workspace()`, `finish_workspace()` | Transferable | [Architecture: the filesystem](docs/architecture.md#the-filesystem) |
| 5 | **Interchangeable coding harnesses.** Claude Code and the Antigravity CLI share one protocol, configured via native files (`~/AGENTS.md`, skills, MCP config, deny rules). | [`app/workers/harnesses/base.py`](app/workers/harnesses/base.py) → `CodingHarness`, `CliHarness.thread_files()`, [`registry.py`](app/workers/harnesses/registry.py) → `get_harness_registry()`, [`sandbox/home/AGENTS.md`](sandbox/home/AGENTS.md), [`sandbox/skills/`](sandbox/skills/) | Transferable | [Extending: add a coding harness](docs/extending.md#add-a-coding-harness) |
| 6 | **Multi-turn threads with gates.** Work runs in a multi-turn thread (`Turn` list) with statuses `running`, `awaiting_approval`, and `idle`, and outcomes `finished`, `failed`, and `cancelled`. Prompts are delegated verbatim; questions and plans are normal turn answers. Gated MCP calls pause at `awaiting_approval`: voice approvals follow a two-turn prompt rule, while UI taps send `permission_key`. | [`app/threads/registry.py`](app/threads/registry.py) → `ThreadRegistry`, [`app/threads/driver.py`](app/threads/driver.py) → `drive()` | Any human-in-the-loop agent | [Architecture: thread lifecycle](docs/architecture.md#thread-lifecycle) |
| 7 | **One MCP gateway with server-held tokens.** Harnesses see one MCP server. The gateway proxies [`.mcp.json`](.mcp.json) servers as `<server>__<tool>`, attaching the user's token on the server so no OAuth token reaches a computer. Connected apps define access; `mcp_gates` pauses writes for approval. Built-in stdio servers cover Google REST APIs. | [`app/api/mcp_gateway.py`](app/api/mcp_gateway.py) → `call_gateway_tool()`, [`app/api/mcp_permissions.py`](app/api/mcp_permissions.py) → `request_permission()`, [`workspace_mcp/`](workspace_mcp/) → `Server` in [`_server.py`](workspace_mcp/_server.py) | Any agent calling MCP servers for a user | [Security model: the MCP gateway](docs/security-model.md#the-mcp-gateway) |
| 8 | **Declarative permissions.** [`config/policy.yaml`](config/policy.yaml) declares shell `deny` globs (rendered into native harness configs), `mcp_gates` enforced at the gateway, and run budgets. | [`app/workers/policy.py`](app/workers/policy.py) → `load_policy()`, [`claude.py`](app/workers/harnesses/claude.py) → `permission_settings()`, [`antigravity.py`](app/workers/harnesses/antigravity.py) → `deny_rules()` | Transferable | [Security model: permission gates](docs/security-model.md#permission-gates) |
| 9 | **Context and answer cards alongside thread chat.** [`app/api/surfaces.py`](app/api/surfaces.py) serves [A2UI v0.9](https://github.com/google/A2UI) cards (`events`, `threads`, `files`), projects render as thread feeds, and threads render as multi-turn chat in [`ThreadView.tsx`](web/src/components/stage/ThreadView.tsx) using shared parts ([`web/src/components/stage/thread/`](web/src/components/stage/thread/)). [`app/api/mcp_outputs.py`](app/api/mcp_outputs.py) extracts links to created PRs, Docs, and events. | [`app/api/surfaces.py`](app/api/surfaces.py) → `build_a2ui_surfaces()`, [`app/context_cards.py`](app/context_cards.py) → `card_items()`, [`web/src/components/stage/ThreadView.tsx`](web/src/components/stage/ThreadView.tsx), [`web/src/components/a2ui/A2UISurfaceDeck.tsx`](web/src/components/a2ui/A2UISurfaceDeck.tsx), [`web/src/components/a2ui/answer/AnswerCard.tsx`](web/src/components/a2ui/answer/AnswerCard.tsx) | Transferable | [Architecture: the visual stage](docs/architecture.md#the-visual-stage) |
| 10 | **Declarative connected apps.** Google and GitHub are declared in YAML (OAuth, icons, services, `mcp_servers`, scopes) with a `ProviderHooks` seam for provider-specific flows. `make onboard` registers them once; users connect via OAuth 2.0 (+ PKCE for Google) with refresh rotation. | [`config/integrations.yaml`](config/integrations.yaml), [`app/auth/oauth.py`](app/auth/oauth.py) → `ensure_fresh_access_token()`, [`app/auth/provider_hooks.py`](app/auth/provider_hooks.py) → `ProviderHooks`, [`app/api/apps.py`](app/api/apps.py) → `apps_state()` | Pattern transfers | [Extending: add an app](docs/extending.md#add-an-app) |

## Beyond the core

- **Briefing a returning user.** [`app/voice/briefing.py`](app/voice/briefing.py) → `collect_missed_milestones()` and [`app/voice/instruction.py`](app/voice/instruction.py) → `build_orchestrator_instruction()` summarize what happened while the user was away. `build_connections_briefing()` lists unconnected apps under `NOT CONNECTED` so the voice opens the Connect page via `navigate` ([`app/tools/navigation_tools.py`](app/tools/navigation_tools.py)) and resumes on `[APP CONNECTED] <name>`.
- **Quick reads without a thread.** [`app/tools/look_tools.py`](app/tools/look_tools.py) → `look()` runs in-process [`workspace_mcp`](workspace_mcp/) tools marked `readOnlyHint` and shows an answer card on home. Routing rules live in [`app/voice/instruction.py`](app/voice/instruction.py) and `tests/eval/datasets/system1.json`.
- **ADK Voice Agent's own tools over MCP.** [`app/api/mcp.py`](app/api/mcp.py) serves `list_threads`, `read_thread`, `update_user`, `save_to_drive`, `present_card`, `recall`, `recall_conversation`, and grounded search at `/api/v1/mcp` with a per-thread token (`mint_mcp_token()`), reached via the daemon's `/mcp-relay` and documented in [`connected-apps-mcp`](sandbox/skills/connected-apps-mcp/SKILL.md).
- **Durable event log.** [`app/store/thread_store.py`](app/store/thread_store.py) → `ThreadStore` stores thread rows, append-only events, and ADK sessions (`get_session_service()` in [`app/app_utils/services.py`](app/app_utils/services.py)) in SQLite or Postgres.
- **Remembering the user.** [`app/memory/`](app/memory/) keeps one structured profile per user in Agent Engine Memory Bank: `store.py` → `recall_profile()`, `profile.py` → `render_profile()`, `ingest.py` → `curated_contents()` (user speech, agent replies, and `start_thread`, `message_thread`, `share_context` arguments; never tool output), and `episodes.py` for conversation/thread memories. See [Architecture: memory](docs/architecture.md#memory) and [Security model: memory](docs/security-model.md#memory).
- **Carrying context between conversations and threads.** [`app/voice/continuity.py`](app/voice/continuity.py) carries recent turns across dropped connections; `share_context` in [`app/threads/registry.py`](app/threads/registry.py) and [`app/threads/prompts.py`](app/threads/prompts.py) → `share_context_block()` passes prompts, answers, changed files, and branch between threads. See [Architecture: sharing context between threads](docs/architecture.md#sharing-context-between-threads).
- **Screen focus and project threads.** [`app/voice/focus.py`](app/voice/focus.py) tracks screen focus (`home`, `project`, `thread`) via `POST /api/v1/focus` and `navigate` ([`app/tools/navigation_tools.py`](app/tools/navigation_tools.py)), injecting `OPEN ON SCREEN` and pushing live `[SCREEN FOCUS]` updates. [`app/projects.py`](app/projects.py) → `project_thread()` lists a project's threads and reads its status note from a running computer (else from the newest thread in the store); `name_project()` promotes `scratch/` to `projects/`. See [Architecture: screen focus and projects](docs/architecture.md#screen-focus-and-projects).
- **Test doubles at the boundaries only.** Tests swap the environment (`set_environment`), put a scripted `claude` / `agy` on `PATH` ([`tests/fake_agent_cli.py`](tests/fake_agent_cli.py)), and inject HTTP transports ([`tests/fake_integrations.py`](tests/fake_integrations.py)).
- **One container for System 1.** [`app/fast_api_app.py`](app/fast_api_app.py) serves `/run_live`, REST, and the built UI; computers run [`sandbox/Dockerfile`](sandbox/Dockerfile).

## Study order

1. [`app/agent.py`](app/agent.py), then [`app/voice/`](app/voice/): `non_blocking.py`, `thread_updates.py`, `focus.py`, `instruction.py`.
2. [`app/threads/`](app/threads/): follow `start_thread()` in [`app/tools/thread_tools.py`](app/tools/thread_tools.py) to `executing` (`status="running"`), through `awaiting_permission` (`status="awaiting_approval"`) and `approve()`, to `completed` (`status="idle"`, `outcome="finished"`) and `message_thread()`.
3. [`app/workers/workspace.py`](app/workers/workspace.py): folder selection, leases, worktrees, and per-turn commits.
4. [`app/workers/providers/`](app/workers/providers/), [`app/workers/environment.py`](app/workers/environment.py), and [`sandbox/README.md`](sandbox/README.md).
5. [`app/api/mcp_gateway.py`](app/api/mcp_gateway.py), [`app/api/mcp_permissions.py`](app/api/mcp_permissions.py), and [`config/policy.yaml`](config/policy.yaml).
6. [`app/api/surfaces.py`](app/api/surfaces.py), [`web/src/components/stage/ThreadView.tsx`](web/src/components/stage/ThreadView.tsx), and [`web/src/components/a2ui/`](web/src/components/a2ui/).
7. [`app/auth/`](app/auth/) and [`config/integrations.yaml`](config/integrations.yaml).

## What to skip when studying

- Setup & deploy: `scripts/onboard.py`, `scripts/doctor.sh`, `scripts/provisioning/`, `scripts/deploy.py`, `terraform/`.
- UI chrome & first-boot plumbing: `web/src/components/sheets/`, `web/src/components/stage/`, `sandbox/entrypoint.sh`.

## agents-cli metadata

[`agents-cli-manifest.yaml`](agents-cli-manifest.yaml) declares `voice_agent` in `app/agent.py`; [`manifest.yaml`](manifest.yaml) is the adk-recipes catalog entry.

---

# Maintaining the code

## Keep this file current

When moving, renaming, or adding a module, update the recipe table, layout, and linked docs in `docs/`. All file and symbol links must resolve.

## Rules

- Only change code the request targets. Reuse existing protocols or registries over per-provider special cases.
- Run Python through `uv run`.
- Never change model values (`LIVE_MODEL`, `GROUNDING_MODEL`, harness models) unless asked; model reads go through [`app/app_utils/models.py`](app/app_utils/models.py). Fix a `404` by checking `LIVE_MODEL_LOCATION` or `GOOGLE_CLOUD_LOCATION` (`global`).
- No test-mode branches in `app/`. Test doubles live in `tests/fakes.py`, `tests/fake_agent_cli.py`, `tests/fake_integrations.py`, and `tests/eval/sitecustomize.py`.
- Keep `app/agent.py` to wiring; put voice logic in `app/voice/` and thread logic in `app/threads/`.
- New thread states/transitions go in `TRANSITIONS` in [`app/threads/model.py`](app/threads/model.py), with wording in `describe.py`.
- New environment variables go in [`.env.example`](.env.example) and [`docs/configuration.md`](docs/configuration.md).
- UI styles use tokens in [`web/src/styles/base.css`](web/src/styles/base.css), never hex literals.
- Never block on stdin as an agent: pass `NONINTERACTIVE=1` (or `--no-input`) with `make onboard PROJECT=<id> NONINTERACTIVE=1`, and run `make deploy DRY_RUN=1` before `make deploy AUTO_APPROVE=1 NONINTERACTIVE=1`. See [Run as a coding agent](docs/quickstart.md#run-as-a-coding-agent).
- Import ADK tool instances, not modules: `from google.adk.tools.load_web_page import load_web_page`.
- Verify voice changes with `make eval` (`uv run agents-cli eval compare <baseline> <candidate>`), then `make test` and `make lint`.
- Deploy only when asked (`make deploy DRY_RUN=1` first). On a terraform `409`, `terraform import` the resource.
- If an error repeats three times, fix the root cause instead of retrying.

## Layout

```text
adk-voice-agent/
├── app/
│   ├── agent.py              # root_agent `voice_agent`: wiring only
│   ├── fast_api_app.py       # FastAPI app: ADK /run_live, REST, built UI, lifespan
│   ├── voice/                # non_blocking, thread_updates, voice_mode (VoiceSessionPlugin, live_run_config, VoiceAgent), focus (OPEN ON SCREEN), instruction, briefing, callbacks, background, preferences, continuity
│   ├── threads/              # model (states, Turn, TRANSITIONS), registry, driver, describe, prompts, titles, summaries, retention, documents (save_to_drive)
│   ├── store/                # ThreadStore, UserSettingsStore, SQLAlchemy models and engine
│   ├── memory/               # profile (UserProfile, render_profile), store (MemoryStore, MemoryBankStore, recall_profile), ingest (curated_contents, remember_on_close), episodes
│   ├── workers/
│   │   ├── environment.py    # Environment protocol: SandboxEnvironment, LocalEnvironment
│   │   ├── factory.py        # resolve_environment(), set_environment()
│   │   ├── providers/        # ComputerProvider: host, docker, static, gce; registry; reaper; last_known
│   │   ├── runs.py           # detached harness runs: launch, poll, cancel
│   │   ├── workspace.py      # home projects (code/, projects/, scratch/), leases, worktrees, commits, push
│   │   ├── policy.py         # declarative permissions and run budgets (config/policy.yaml)
│   │   ├── manifest.py       # config/workspaces.yaml loader
│   │   └── harnesses/        # CodingHarness, CliHarness, claude, antigravity, registry, activity
│   ├── api/                  # REST under /api/v1, MCP bridge (/api/v1/mcp), mcp_gateway, mcp_permissions, mcp_outputs, surfaces, projects, apps, auth, memories
│   ├── auth/                 # identity (dev / IAP), credential_store, oauth, providers, provider_hooks, google_apis, oauth_popup, adc, sandbox_credentials, env_store, verify, github/
│   ├── tools/                # thread_tools, navigation_tools (navigate), grounding_tools, look_tools (look), memory_tools
│   ├── app_utils/            # services, models (model env readers), http_client, time_utils, env_flags, model_locations, user_context, host_guard
│   ├── projects.py           # list_projects, project_thread, delete_project_history, ProjectLabels, name_project
│   ├── user_settings.py      # per-user voice preferences and default harness
│   └── context_cards.py      # context cards and answer-card kinds (CARD_KINDS, card_items)
├── config/                   # integrations.yaml, policy.yaml, workspaces.yaml
├── .mcp.json                 # connected apps' MCP servers, proxied by the gateway
├── workspace_mcp/            # built-in stdio Google Workspace MCP servers (_server.py, _google.py)
├── docs/                     # quickstart, computer, connected-apps, architecture, configuration, security-model, extending, evals
├── scripts/                  # onboard.py, provisioning/, doctor.sh, computer_ssh.py, deploy.py, run_eval.py, dev.sh
├── sandbox/                  # computer image: Dockerfile, entrypoint.sh, home/, skills/, server.py
├── terraform/                # Cloud Run + IAP, Secret Manager, Cloud SQL, Agent Engine, computer VPC/NAT/template/SA
├── tests/                    # unit/, live/, eval/ (voice/ System 1, system2_agent/ System 2, datasets/, fixtures/alex), test_runnability.py, fakes
├── web/                      # React + TypeScript + Vite UI (src/components/a2ui/answer/, src/lib/cards.ts)
├── Dockerfile                # System 1 image: built UI + API
├── Makefile                  # run `make` for targets
├── manifest.yaml             # adk-recipes catalog entry
└── agents-cli-manifest.yaml  # agents-cli project config
```

## Commands

| Command | Purpose |
|---|---|
| `make onboard` | Set up a deployment: `.env`, app registrations, terraform, computer image, and deploy (`PROJECT=<id>`, `DRY_RUN=1`) |
| `make dev` | API on :8000 and UI on :3000 with hot reload |
| `make docker` | Build and run System 1's image on :8080 |
| `make deploy` | Terraform, then `agents-cli deploy` to Cloud Run behind IAP (`DRY_RUN=1`, `AUTO_APPROVE=1`) |
| `make destroy` | Delete computers (`PURGE=1` for home disks) and terraform resources |
| `make doctor` | Check your computer over IAP SSH: tools, daemon, Vertex AI, MCP relay, disk |
| `make test` | Offline unit, runnability, and web tests |
| `make lint` | ruff 0.16.8, codespell, ty, mypy, Biome 2.5.10, and the web typecheck |
| `make format` | Ruff and Biome formatting with the adk-recipes rules |
| `npm --prefix web test` | Web unit tests |
| `make ssh` | Shell in your computer (`gcloud compute ssh --tunnel-through-iap`, then `docker exec`) |
| `make eval` | System 1 voice evals over ADK Live against a scripted computer (`DATASET=`, `CASES=`) |
| `make eval-sandbox` | System 2 computer evals on docker computers with Alex persona (`HARNESS=`, `CASES=`, `NO_BUILD=1`) |
| `make computer-image` | Build multi-arch computer image ([`sandbox/Dockerfile`](sandbox/Dockerfile), [`sandbox/cloudbuild.yaml`](sandbox/cloudbuild.yaml)) and print rollout tag ([`terraform/README.md`](terraform/README.md)) |

REST routes: [Architecture: REST control plane](docs/architecture.md#rest-control-plane).
Troubleshooting: [Quickstart](docs/quickstart.md#troubleshooting), [Your computer](docs/computer.md#troubleshooting), [Connected apps](docs/connected-apps.md#troubleshooting), [Evals](docs/evals.md#troubleshooting).
