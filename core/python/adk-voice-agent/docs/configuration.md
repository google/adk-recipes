# Configuration

Configuration lives in `.env` at the repo root. `make onboard` creates it from [`.env.example`](../.env.example), which declares every variable the code reads with its default value populated (and `GOOGLE_CLOUD_PROJECT=<TODO: update-this-value>`). [`app/__init__.py`](../app/__init__.py) calls `load_dotenv_defaults()`, which loads `.env` and then fills any variable it leaves unset from `.env.example` (skipping blank values and `<TODO: ...>` placeholders; not done when `VOICE_AGENT_DOTENV_PATH` points elsewhere), and model names are read through [`app/app_utils/models.py`](../app/app_utils/models.py) with no hardcoded literal fallbacks in Python code. Values already in the process environment take precedence.

In this doc:

- [Google Cloud and the voice agent](#google-cloud-and-the-voice-agent)
- [Where coding threads run](#where-coding-threads-run)
- [Connected apps](#connected-apps)
- [Server and storage](#server-and-storage)
- [Make variables](#make-variables)

## Google Cloud and the voice agent

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | — | GCP project for Vertex AI and computers. Required (`<TODO: update-this-value>` in `.env.example`). |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI location for text models; keep `global`. Computers use `SANDBOX_GCP_LOCATION` instead. |
| `LIVE_MODEL_LOCATION` | `us-central1` | Regional Vertex AI location for `LIVE_MODEL` ([`app/app_utils/model_locations.py`](../app/app_utils/model_locations.py)). |
| `GOOGLE_GENAI_USE_ENTERPRISE` | `true` | Use Vertex AI with ADC (defaulted from `.env.example` even when `.env` omits it). Computers receive both this and `GOOGLE_GENAI_USE_VERTEXAI`. |
| `LIVE_MODEL` | `gemini-3.8-live` | Gemini Live model for the voice agent ([`app/agent.py`](../app/agent.py), [`app/app_utils/models.py`](../app/app_utils/models.py)). |
| `GROUNDING_MODEL` | `gemini-3.5-flash-lite` | Model for Search, Maps, and turn summaries (`thinking_level=MINIMAL`). |
| `LIVE_VOICE_NAME` | `Aoede` | Default voice persona ([Settings per user](security-model.md#settings-per-user)). |
| `VOICE_MODE` | `native` | Default voice mode: `native` or `cascaded` ([Voice modes](architecture.md#voice-modes)). |
| `CASCADE_STT_MODEL` | `gemini-3.5-transcribe-live` | Cascaded speech-to-text model (always called in `global`). |
| `CASCADE_LLM_MODEL` | `gemini-3.5-flash-lite` | Cascaded text model (`thinking_level=MINIMAL`, called in `GOOGLE_CLOUD_LOCATION`). |
| `CASCADE_TTS_MODEL` | `gemini-3.8-flash-tts` | Cascaded text-to-speech model (always called in `global`). |
| `USER_TIMEZONE` | `America/Chicago` | Default IANA timezone; overridable per user in Settings → Timezone. |
| `GOOGLE_CLOUD_AGENT_ENGINE_ID`, `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION` | set by `make onboard` / `make deploy` | Agent Engine resource name for Memory Bank ([Memory](architecture.md#memory), [`terraform/memory.tf`](../terraform/memory.tf)). `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION` is only used when the ID is bare. |
| `MEMORY_MODEL` | `GROUNDING_MODEL` | Model for conversation recaps ([Memory: episodes](architecture.md#episodes-conversations-and-threads)). |
| `VOICE_AGENT_CARRYOVER_MINUTES` | `10` | Max minutes since the previous conversation ended to carry over its last turns (`0` disables; [Continuity](architecture.md#continuity-across-connections)). |
| `LOGS_BUCKET_NAME` | — | Optional GCS bucket for ADK artifacts. |

## Where coding threads run

Each user's threads run on their own computer ([Architecture: where code runs](architecture.md#where-code-runs)).

| Variable | Default | Purpose |
|---|---|---|
| `CODING_HARNESS` | `claude` | Default harness (`claude` or `antigravity`). |
| `MAX_RUNNING_THREADS_PER_USER` | `3` | Max concurrent `executing` threads per user (server-wide cap is 20). |
| `COMPUTER_PROVIDER` | `host` | Provider ([`app/workers/providers/registry.py`](../app/workers/providers/registry.py)): `host`, `docker`, `static`, or `gce`. |
| `COMPUTER_IMAGE` | `adk-voice-agent-computer:dev` | `docker`: container image for `adk-computer-<owner>`. |
| `COMPUTER_URL_TEMPLATE` | — | `static`: per-user daemon URL with `{owner}` placeholder. |
| `COMPUTER_URL` | — | `static`: shared single-user daemon URL. |
| `COMPUTER_INSTANCE_TEMPLATE` | set by `terraform` | `gce`: instance template name or self link. |
| `COMPUTER_ZONE` | set by `terraform` | `gce`: zone for VMs and disks. |
| `COMPUTER_DISK_GB` | `50` | `gce`: size of new `<COMPUTER_DEPLOYMENT>-<owner>-home` disks. |
| `COMPUTER_SERVICE_ACCOUNT` | template's | `gce`: computer VM service account. |
| `COMPUTER_SNAPSHOT_POLICY` | — | `gce`: snapshot schedule policy for home disks. |
| `COMPUTER_DEPLOYMENT` | `adk-voice-agent` | `gce`: deployment label and prefix (`<deployment>-<owner>`). |
| `COMPUTER_MCP_AUDIENCE` | set by `terraform` | `gce`: IAP audience signed by the computer SA for `/mcp-relay`. |
| `COMPUTER_IDLE_STOP_MIN` | `20` | `docker` / `gce`: idle minutes before stopping (`0` disables). |
| `COMPUTER_WARM_HOURS` | `08:00-18:00` | `docker` / `gce`: daily warm window in user's timezone (`off` disables; ~$0.70/day or ~$15/month on `e2-standard-2`). |
| `COMPUTER_WARM_DAYS` | `mon-fri` | Days the warm window applies (`mon`…`sun`). |
| `COMPUTER_TIMEZONE` | `USER_TIMEZONE`, else `America/Chicago` | Fallback timezone for warm hours. |
| `DOTFILES_REPO` | — | `docker` / `gce`: dotfiles repo cloned to `~/.dotfiles` on first boot; `install.sh` runs on every boot. |
| `SANDBOX_GCP_PROJECT` | `GOOGLE_CLOUD_PROJECT` | GCP project passed to harness runs on the computer. |
| `SANDBOX_GCP_LOCATION` | `global` | Vertex AI location passed to harness runs on the computer. |
| `SANDBOX_CALLER_SA` | `adk-voice-agent@<project>.iam.gserviceaccount.com` | System 1 service account allowed by `gce` daemons. |
| `GIT_AUTHOR_NAME`, `GIT_COMMITTER_NAME` | `ADK Voice Agent` | Git author/committer name on per-turn commits. |
| `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_EMAIL` | `voice-agent@users.noreply.github.com` | Git author/committer email on per-turn commits. |
| `SANDBOX_WORKSPACE` | `/workspace` (`/home/agent` in image) | Root directory enforced by [`sandbox/server.py`](../sandbox/server.py). |
| `COMPUTER_REQUIRE_AUTH` | off | Daemon flag requiring caller ID tokens on all routes except `/healthz`. |
| `COMPUTER_AUTH_AUDIENCE` | `adk-voice-agent-computer` | Base audience for daemon ID tokens (`static`, `gce`); System 1 scopes tokens per owner as `<COMPUTER_AUTH_AUDIENCE>/<owner>` (verified against `COMPUTER_OWNER` on the VM). |
| `COMPUTER_ALLOWED_CALLERS` | — | Comma-separated caller service account emails accepted by the daemon. |
| `COMPUTER_MCP_UPSTREAM` | set by provider | System 1 base URL used by `/mcp-relay/`. |
| `VOICE_AGENT_MCP_BASE_URL` | relay, else `APP_URL` | Base URL for `/api/v1/mcp` when no relay is used (`host`). |
| `VOICE_AGENT_MCP_DEV_TOKEN` | — | Local-only fixed bearer token for testing `/api/v1/mcp` (ignored in remote mode). |

### Harness models

| Variable | Default | Harness |
|---|---|---|
| `CLAUDE_CODE_USE_VERTEX` | `1` | Route Claude Code through Vertex AI. |
| `CLOUD_ML_REGION` | `global` | Vertex AI region for Claude. |
| `ANTHROPIC_VERTEX_PROJECT_ID` | `GOOGLE_CLOUD_PROJECT` | Vertex AI project for Claude. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude Code primary model. |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `claude-sonnet-5-5` | Claude Code mid-size model. |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `ANTHROPIC_SMALL_FAST_MODEL` | Claude Code background model. |
| `ANTHROPIC_SMALL_FAST_MODEL` | `claude-haiku-5-5` | Fallback name for Claude background model. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `ANTHROPIC_MODEL` | Model for Claude `opus` alias. |
| `CLAUDE_CODE_SUBAGENT_MODEL` | `ANTHROPIC_MODEL` | Model for Claude Code subagents. |
| `VERTEX_REGION_CLAUDE_HAIKU_4_5`, `VERTEX_REGION_CLAUDE_4_6_SONNET` | — | Optional per-model Vertex AI region overrides. |
| `ANTIGRAVITY_MODEL` | `gemini-4-argon` | Antigravity CLI model. |
| `ANTIGRAVITY_EFFORT` | `medium` | Antigravity `--effort` level (`low`, `medium`, `high`). |
| `AGY_ADC_AUTH` | `1` | Authenticate Antigravity via Application Default Credentials. |

## Connected apps

Declared under `apps` in [`config/integrations.yaml`](../config/integrations.yaml) and registered once by `make onboard` ([Connected apps: set up](connected-apps.md#set-up-once-in-make-onboard)):

| Variable | Set by | Purpose |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | `make onboard` | Google Web OAuth client (`<APP_URL>/api/v1/auth/google_workspace/callback` and `http://127.0.0.1:8000/...`). Terraform enables the Workspace and MCP APIs (`enable_workspace_apis`). |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | `make onboard` | GitHub App OAuth client credentials. |
| `GITHUB_APP_SLUG` | `make onboard` | GitHub App URL slug (`github.com/apps/<slug>`). |

Per-user credentials are stored automatically on Connect ([Connected apps per user](security-model.md#connected-apps-per-user)):

| App | Per-user keys |
|---|---|
| Google | `GOOGLE_WORKSPACE_ACCESS_TOKEN`, `GOOGLE_WORKSPACE_REFRESH_TOKEN`, `GOOGLE_WORKSPACE_ACCOUNT`, `GOOGLE_WORKSPACE_GRANTED_SCOPES`, `GOOGLE_WORKSPACE_TOKEN_SOURCE` |
| GitHub | `GITHUB_ACCESS_TOKEN`, `GITHUB_REFRESH_TOKEN`, `GITHUB_ACCOUNT` |

- **Saving to Drive:** `drive.save_project_files` (`on` by default) enables `save_to_drive` into `ADK Voice Agent/<project>`.
- **Connected apps in threads:** proxied from [`.mcp.json`](../.mcp.json) and gated by `mcp_gates` in [`config/policy.yaml`](../config/policy.yaml) ([The MCP gateway](security-model.md#the-mcp-gateway)).

## Server and storage

| Variable | Default | Purpose |
|---|---|---|
| `THREAD_DB_URL` | SQLite in state dir | Thread, settings, and ADK session DB URL (falls back to `DATABASE_URL`, `SESSION_DB_URL`; Cloud SQL when `enable_cloud_sql` is on). |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `2`, `3` | Postgres pool sizing. |
| `SQL_DEBUG` | off | Log SQL statements. |
| `VOICE_AGENT_STATE_DIR` | `~/.adk-voice-agent` | Local state directory. |
| `VOICE_AGENT_WORKSPACE_ROOT` | `~/.adk-voice-agent/workspaces` | `host` provider user homes (`u-<hash>`). |
| `VOICE_AGENT_WORKSPACES_LOCAL_YAML` | `config/workspaces.local.yaml` | Local repo manifest override ([Your computer: add a repo](computer.md#add-a-repo)). |
| `VOICE_AGENT_DOTENV_PATH` | `.env` | Alternate `.env` path (shell-only). |
| `VOICE_AGENT_LOAD_DOTENV` | `1` | `0` skips loading `.env` (shell-only). |
| `VOICE_AGENT_SKIP_STARTUP_WARMUP` | off | Skip local Google token refresh at startup. |
| `ALLOW_ORIGINS` | dev origins; `APP_URL` deployed | Comma-separated CORS origins. |
| `APP_URL` | — | Public base URL (required when deployed). |
| `<PROVIDER>_REDIRECT_URI` | from `APP_URL` | Override a provider's OAuth redirect URI. |
| `VOICE_AGENT_WEB_DIST` | `web/dist` | Built frontend directory. |
| `VOICE_AGENT_BIND_HOST` | — | Uvicorn bind address (`$(HOST)` in `make dev`). |
| `VOICE_AGENT_ALLOWED_HOSTS` | — | Extra allowed `Host` names when bound to `0.0.0.0`. |
| `VOICE_AGENT_AUTH_MODE` | `dev` | `dev` locally or `iap` deployed ([Identity](security-model.md#identity)). |
| `VOICE_AGENT_DEV_USER_ID` | `local` | User ID in `dev` mode. |
| `VOICE_AGENT_IAP_AUDIENCE` | — | IAP JWT audience (`/projects/<number>/locations/<region>/services/<name>`). |
| `VOICE_AGENT_CREDENTIAL_STORE` | `auto` | `auto` (Secret Manager in remote mode, `.env` locally), `env`, or `secret_manager` (`<COMPUTER_DEPLOYMENT>-user-<hash>` secrets; [Connected apps per user](security-model.md#connected-apps-per-user)). |

Cloud Run sets `K_SERVICE` / `CLOUD_RUN_JOB` ([remote mode](security-model.md#local-and-deployed-modes)), and the driver sets `VOICE_AGENT_THREAD_CLONE` per run.

## Make variables

Read by [`Makefile`](../Makefile):

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT` | `.env` or gcloud config | GCP project for `onboard`, `deploy`, `destroy`, `computer-image`, `ssh`, `doctor`. |
| `REGION` | `us-central1` | Region for Cloud Run, Artifact Registry, and computer network. |
| `DRY_RUN` | — | `1`: preview `onboard` / `deploy` without changes. |
| `AUTO_APPROVE` | — | `1`: skip terraform approval prompt in `make deploy`. |
| `NONINTERACTIVE` | `1` when `CI` is set | `1`: never prompt on stdin in `onboard` / `deploy` (`--no-input`). |
| `COMPUTER_IMAGE` | resolved | Computer image tag for `make deploy`. |
| `PURGE` | — | `1`: also delete user home disks in `make destroy`. |
| `SKIP_GOOGLE`, `SKIP_GITHUB` | — | `1`: skip app registration in `make onboard` ([`scripts/onboard.py`](../scripts/onboard.py)). |
| `GITHUB_ORG` | user account | Organization for GitHub App creation (`--github-org`). |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_APP_SLUG` | `.env` | Non-interactive app credentials for `make onboard`. |
| `TF_WORKSPACE`, `TFVARS` | default; `terraform/terraform.tfvars` | Terraform workspace and variables file ([`terraform/README.md`](../terraform/README.md#state)). |
| `HOST`, `API_PORT`, `WEB_PORT` | `127.0.0.1`, `8000`, `3000` | Bind address and ports for `make dev` / `make docker` ([`scripts/dev.sh`](../scripts/dev.sh)). |
| `DATASET`, `CASES` | suite defaults | Eval dataset and case filter for `make eval` / `make eval-sandbox`. |
| `HARNESS`, `NO_BUILD` | `claude,antigravity`, — | Harnesses and image reuse flag for `make eval-sandbox`. |

## Where to go next

- [Quickstart](quickstart.md).
- [Your computer](computer.md).
- [Connected apps](connected-apps.md).
- [Security model: local and deployed modes](security-model.md#local-and-deployed-modes).
