# Configuration reference

Catalog of environment variables and dependency extras.
`.env` is loaded by `make dev-backend` (`set -a; . ./.env; set +a`); start from
[`../.env.example`](../.env.example). Deeper rationale lives in [`../AGENTS.md`](../AGENTS.md);
the per-layer security model in [`security-model.md`](security-model.md).

> **No local model.** Every chat calls Vertex for inference. `LHA_ENVIRONMENT_BACKEND=local`
> only moves *tool execution* to your host.

## In this doc

- **Routes** — what the served app mounts.
- **Environment variables** — the full catalog, grouped by subsystem (core/Vertex, identity, model, sandbox, sessions, memory, scheduler, OAuth, …).
- **Dependency extras** — the optional `lha[…]` extras and what each pulls in.

---

## Routes

The served app (`horizon.fast_api_app:app`) mounts every router: A2A + `/lha/*`
(sessions, state, tasks, memories, uploads, sandbox, processes, secrets, routines),
`/feedback`, the OAuth callbacks, and the `/scheduler/*` endpoints. To ship a subset,
delete the `attach_*` calls you don't want in `horizon/fast_api_app.py`. What each
route exposes — and the credentials the `secrets`/`oauth` routes inject — is in
[`security-model.md`](security-model.md).

> **Feedback stays in your project.** The `feedback` surface (the UI's
> "Have a suggestion?" button → `POST /feedback`) writes a structured record to
> **your own Cloud Logging** (`horizon/feedback/sink.py`), with a stdlib-log
> fallback. It is never sent to the project authors or any external endpoint.

---

## Environment variables

### Core / Vertex (required for any chat)

| Var | Default | Notes |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | active `gcloud` config | Blank ⇒ falls back to ADC project (`make dev` fills it in). |
| `GOOGLE_CLOUD_LOCATION` | `global` | Set via import-time `setdefault` — a value you export wins. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `True` | Import-time `setdefault`; a value you export wins. |

### Identity / auth

| Var | Default | Notes |
|---|---|---|
| `LHA_AUTH_MODE` | `dev` | `dev` (every request = `LHA_DEV_USER_ID`) · `iap` (verify IAP JWT) · `trusted_header` (verbatim `X-LHA-User-Id`). `dev` refuses to run when `K_SERVICE` is set. |
| `LHA_DEV_USER_ID` | `dev@local` | Identity all requests collapse to under `dev`. |
| `LHA_IAP_AUDIENCE` | — | Required under `iap`; the Cloud Run audience. Unset ⇒ 500. |

### Model

| Var | Default | Notes |
|---|---|---|
| `LHA_ROOT_MODEL` | `gemini-3.7-flash` | Root-agent default; a key in `horizon/models/registry.py`. `/model` overrides per-session. |
| `LHA_VERTEX_SERVICE_TIER` | _(off)_ | Set to `priority` to pin Gemini to Vertex's `SERVICE_TIER_PRIORITY` per turn. **Off by default** — the tier needs a Vertex entitlement most projects lack. |

### Environment / sandbox

| Var | Default | Notes |
|---|---|---|
| `LHA_ENVIRONMENT_BACKEND` | `local` | `local` (tools on host) or `sandbox` (managed Sandboxes). |
| `LHA_SANDBOX_INTERNET_ACCESS` | _(dev `1`, deployed `0`)_ | Sandbox outbound internet. `0` = hermetic (no egress); `1` = internet-on. Unset, it defaults to **on in local dev** and **off when deployed** (Cloud Run sets `K_SERVICE`), so `make dev` works out of the box while prod stays default-deny. Egress is baked into the sandbox at create time — changing this takes effect on the next sandbox provision, not live. |
| `LHA_RUNTIME_IMAGE` | — | Sandbox BYOC image (only under `sandbox`). |
| `LHA_SANDBOX_CALLER_SA` | — | Service account for sandbox provisioning. |
| `LHA_SANDBOX_LOCATION` | `us-central1` | Agent Platform region for sandbox provisioning (independent of `GOOGLE_CLOUD_LOCATION`). |
| `LHA_RUNTIME_MIN_VERSION` | _(off)_ | Force-upgrade sandboxes below this floor on reattach. |

### Sessions / storage

| Var | Default | Notes |
|---|---|---|
| `USE_IN_MEMORY_SESSION` | _(off)_ | `true` ⇒ in-process sessions (lost on restart); skips Agent Platform Sessions / Cloud SQL. |
| `SESSION_DB_URL` | — | sqlite/postgres/mysql session store; precedence over Agent Platform Sessions. |
| `AGENT_ENGINE_RESOURCE_NAME` | — | Agent Engine resource for sessions **and** Memory Bank. |
| `LHA_MEMORY_BANK_RESOURCE_NAME` | `AGENT_ENGINE_RESOURCE_NAME` | Escape hatch to split memory onto a separate engine. |
| `LOGS_BUCKET_NAME` | — | GCS bucket for durable artifacts (`GcsArtifactService`); unset ⇒ in-memory artifacts. |
| `ARTIFACT_SERVICE_URI` | `gs://$LOGS_BUCKET_NAME` | Explicit artifact-service URI override; precedence over `LOGS_BUCKET_NAME`. Unset + no bucket ⇒ in-memory. |

### Memory / dream-review

| Var | Default | Notes |
|---|---|---|
| `LHA_DREAM_REVIEW` | on | `0` disables dream-review on all paths. |
| `LHA_DREAM_SESSION_LIMIT` | `50` | Max recent sessions per user fed to profile consolidation. |
| `LHA_DREAM_LOOKBACK_HOURS` | `24` | Activity window for auto-discovering users in `/scheduler/dream-review`. |
| `LHA_MEMORY_CONSOLIDATION` | on | `0` disables dedupe/contradiction reconciliation of general memories. |
| `LHA_LIVE_MEMORY_TESTS` | _(off)_ | `1` runs the live Memory Bank round-trip smoke. |
| `LHA_REVIEW_FORK` | on | `0` disables the post-turn judge fork (memory + skill curation). |
| `LHA_PRE_COMPRESS_FLUSH` | on | `0` disables the pre-compaction memory flush fork. |
| `LHA_FORK_COOLDOWN` | `120` | Seconds between background forks, per session. |

### Identity (SOUL.md)

`~/.lha/SOUL.md`, if present, replaces `DEFAULT_AGENT_IDENTITY` as the opening line of the
agent's system prompt (`horizon/conversation/soul_loader.py`). It is read once, at
App-build time, as part of assembling `Agent.static_instruction`
(`horizon/conversation/system_prompt.py:build_static_instruction`) — editing it now needs
a process restart to take effect, not just a new session, since the constant prefix is no
longer rebuilt per session.

### Context / compaction

| Var | Default | Notes |
|---|---|---|
| `LHA_PRUNE_TOOL_OUTPUTS` | on | `0` disables zeroing of old large tool-result bodies. |
| `LHA_PRELOAD_MAX_MEMORIES` | `20` | Cap on memories injected into the per-turn `<PAST_CONVERSATIONS>` block. ADK's `search_memory` takes no `top_k`, so without this the block grows with the user's memory count forever. |
| `LHA_PRELOAD_MAX_CHARS` | `4000` | Character cap on the same block, applied after the count cap. |
| `ADK_DISABLE_JSON_SCHEMA_FOR_FUNC_DECL` | unset | Selects ADK's lean declaration path process-wide. The root agent no longer needs it: `context/declaration_compaction.py` scopes the same switch to its own tool builds, worth 2,549 chars of the tool surface on every turn. Set it yourself only if you also want it for the child agents, and only if nothing else in the process would be re-rendered by it. |
| `LHA_COMPACTION_WINDOW_FRACTION` | `0.75` | Fraction (0,1) of the model's input window at which compaction fires. |

### Scheduler / routines

| Var | Default | Notes |
|---|---|---|
| `LHA_ROUTINE_STORE` | `memory` | `memory` (not durable) or `postgres` (needs `LHA_REMINDER_DB_URL`; `lha[scheduler]`). |
| `LHA_REMINDER_DB_URL` | — | Postgres URL for routines + resilient sessions. Name is historical (predates the reminder capability's removal), kept as-is. |
| `LHA_SCHEDULER_SA` | — | Service account whose OIDC token `/scheduler/*` accepts. Unset ⇒ endpoints reject. |
| `LHA_SCHEDULER_AUDIENCE` | — | Expected `aud` on that token. Unset ⇒ endpoints reject. |
| `LHA_SCHEDULER_AUTH_DISABLED` | _(off)_ | Skips scheduler token verification. Local testing only. |

### Snapshot (Phase C, off in code — **on** after `make deploy`)

> The code default is off, but `terraform/cloud_run.tf` sets
> `LHA_SNAPSHOT_ENABLED = "1"` on the deployed backend and
> `terraform/cloud_scheduler.tf` creates the daily job unconditionally. A
> `make deploy` therefore ships with Phase C **on**. Set the env var to `0` in
> `cloud_run.tf` if you don't want it.

| Var | Default | Notes |
|---|---|---|
| `LHA_SNAPSHOT_ENABLED` | _(off locally; `1` on Cloud Run via Terraform)_ | Master switch for the daily snapshot+prune job and restore-on-session-start. |
| `LHA_SNAPSHOT_TTL` | `30d` | Snapshot retention TTL. |
| `LHA_SNAPSHOT_KEEP` | `2` | Snapshots kept per user. |

### Secrets

| Var | Default | Notes |
|---|---|---|
| `LHA_SECRET_BACKEND` | `secretmanager` | Per-user secret store backend: `secretmanager`/`gcp` (GCP Secret Manager, needs `GOOGLE_CLOUD_PROJECT`) or `memory` (`InMemorySecretStore`, not durable). Register a custom backend with `set_secret_store()`. |

### Connect Google (OAuth)

Step-by-step client setup: [`quickstart.md`](quickstart.md#6-connect-google-optional-oauth). Bring your own Web OAuth client (the server routes ship in `horizon/auth/oauth.py`); unset ⇒ feature off.

| Var | Default | Notes |
|---|---|---|
| `LHA_GCP_OAUTH_CLIENT_ID` | — | Web OAuth client for the "Connect Google" buttons. Unset ⇒ `/connect` returns 503. Also the audience for GE bearer verification under `iap`. |
| `LHA_GCP_OAUTH_CLIENT_SECRET` | — | HMAC key for signed `state`. |
| `LHA_GCP_OAUTH_REDIRECT_URI` | — | Public `https://<web-host>/lha/gcp/callback` (`http://localhost:3000/...` for local dev). |

---

## Dependency extras

Core (`import horizon` + the default Gemini agent) needs no extra. Provider/subsystem
deps are optional extras in `pyproject.toml`; the dev/test env pulls them all back via
`lha[full]` in the `dev` dependency-group.

| Extra | Pulls | Needed for |
|---|---|---|
| `lha[postgres]` | `asyncpg`, `yoyo-migrations`, `bcrypt` | durable postgres stores (routines, resilient sessions) |
| `lha[scheduler]` | `lha[postgres]` | scheduler durability (`LHA_ROUTINE_STORE=postgres`) |
| `lha[data]` | `pandas`, `matplotlib`, `seaborn` | in-sandbox data analysis (not imported by the harness) |
| `lha[full]` / `lha[all]` | `postgres,scheduler,data` | the full deployed surface |

```bash
uv pip install "lha[full]"        # everything
uv pip install "lha[postgres]"    # minimal slice + durable stores
```

---

## Where to go next

- [`quickstart.md`](quickstart.md) — local-first 5-minute path
- [`extending.md`](extending.md) — lift/adapt the harness
- [`architecture.md`](architecture.md) — the architecture map
- [`commands.md`](commands.md) — slash-command catalog
- [`security-model.md`](security-model.md) — per-layer auth and security model
- [`sandbox-lifecycle.md`](sandbox-lifecycle.md) · [`routines.md`](routines.md) · [`memory.md`](memory.md) — subsystem deep-dives
