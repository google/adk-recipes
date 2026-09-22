# OAuth User-Consent Flow — Agent Runtime + Gemini Enterprise

## Intent

A clone-and-study ADK agent that reads a user's **Google Drive** files on their
behalf, gated by an **OAuth 2.0 user-consent flow**. The agent is deployed to
**Agent Runtime** (Vertex AI Agent Engine) and registered with **Gemini
Enterprise**. The interesting part is **not** the agent (a thin wrapper around
one Drive tool); it's the **OAuth consent plumbing** — the `negotiate_creds()`
three-stage credential resolution that makes the *same* code work in both local
ADK Web UI dev and production Gemini Enterprise, plus the `register-oauth` step
that tells Gemini Enterprise which credentials to prompt with.

## When To Use

- The user needs an agent that calls a **user-scoped** Google API (Drive,
  Gmail, Calendar, …) and must obtain **explicit user consent** via OAuth 2.0.
- The user wants one codebase that works both **locally** (ADK Web UI drives the
  consent screen) and in **production** (Gemini Enterprise injects the token).
- The user wants a worked example of **registering an OAuth authorization
  resource** with the Discovery Engine API and linking it to a deployed agent.

## Eval

- **Scenarios Path**: `tests/eval/evalsets/` (config: `tests/eval/eval_config.json`)
- **Minimum Score**: `rubric_based_final_response_quality_v1` threshold `0.8`
  (LLM judge `gemini-3.5-flash`, rubrics: `relevance`, `helpfulness`)

Cases in `basic.evalset.json` cover a greeting and a Drive-read request; they
exercise the agent's response quality, not a live OAuth exchange. Run with
`make eval` (single evalset) or `make eval-all` (every `*.evalset.json`).

## End-to-end flow

The `negotiate_creds()` three-stage pattern makes one tool work in two
environments:

```
LOCAL (ADK Web UI)                     PRODUCTION (Gemini Enterprise)
------------------                     ------------------------------
user asks to read a Drive file         user asks to read a Drive file
   -> negotiate_creds() Stage 3:          -> Gemini Enterprise sees the agent's
      request_credential() -> consent        authorizationConfig, prompts consent
      screen (OAUTH_CLIENT_ID/SECRET          using the registered auth resource
      from auths.py)                       -> token injected into
   -> user grants drive.readonly            tool_context.state["temp:<AUTH_ID>"]
   -> Stage 2: get_auth_response()        -> negotiate_creds() Stage 1 finds it
      exchanges code -> Credentials          immediately (never hits 2 or 3)
   -> cached in tool_context.state        -> read_drive_file() calls Drive API
      (Stage 1 on next call)
```

1. **Register** OAuth creds once (`make register-oauth`) → an authorization
   resource in the Discovery Engine API.
2. **Deploy** the agent (`make deploy`) → Agent Runtime, ID saved to
   `deployment_metadata.json`.
3. **Link** the deployed agent to Gemini Enterprise **with** the auth resource
   (`make register-gemini-enterprise AUTH_ID_RESOURCE=... GE_APP_ID=...`).
4. At runtime `read_drive_file()` calls `negotiate_creds()`, gets a token, builds
   a `drive v3` client, and returns file content (Docs/Sheets/Slides via
   `export`, everything else via `get_media`).

## Most interesting files to study (in order)

### OAuth consent flow (the heart of this recipe)
1. **`app/tools.py`** — the core of the recipe. `negotiate_creds()` implements
   the **three-stage credential resolution**: **Stage 1** reads a cached/injected
   token from `tool_context.state` (checks both `TOKEN_CACHE_KEY` and the
   `temp:<AUTH_ID>` key Gemini Enterprise injects; handles both a raw access-token
   `str` and a full credential `dict`, refreshing when expired); **Stage 2**
   picks up a completed ADK exchange via `tool_context.get_auth_response()` and
   builds `Credentials` (caching them); **Stage 3** calls
   `tool_context.request_credential()` and returns `{"pending": True, …}` to
   trigger the consent screen. `read_drive_file()` short-circuits on a pending
   dict, else builds a `drive v3` client and exports/downloads by MIME type.
2. **`app/auths.py`** — the OAuth config. Defines `AUTH_SCHEME`
   (`OAuth2`/`authorizationCode` with Google's authorize + token URLs and the
   `SCOPES` dict), `AUTH_CREDENTIAL` (client id/secret from `OAUTH_CLIENT_ID` /
   `OAUTH_CLIENT_SECRET`), and `AUTH_CONFIG`. **`TOKEN_CACHE_KEY` = `AUTH_ID`**
   (default `google-drive-auth`) is the seam: it must match the id registered
   with Gemini Enterprise so Stage 1 finds the injected `temp:<AUTH_ID>` token.
   In production the client id/secret here are **never used** — only
   `TOKEN_CACHE_KEY` and `SCOPES` matter.
3. **`tools/register_oauth.py`** — the standalone **`register-oauth`** step (not
   deployed with the agent). Interactively (or from env) gathers project /
   location / `AUTH_ID` / client id+secret / scopes, then POSTs a
   `serverSideOauth2` **authorization resource** to
   `{endpoint}-discoveryengine.googleapis.com/v1alpha/.../authorizations`. Note
   it builds an `authorizationUri` with **`access_type=offline`** and
   `prompt=consent` (required — a bare URL is rejected), and handles a `409` by
   delete-and-recreate. This resource is what Gemini Enterprise uses to prompt
   the user; its full resource name feeds `register-gemini-enterprise`.

### Deployment & Gemini Enterprise wiring
4. **`app/agent_engine_app.py`** — wraps `app.agent.app` in an `AdkApp` subclass
   (`AgentEngineApp`) for Agent Runtime: `set_up()` initialises `vertexai` +
   telemetry + Cloud Logging, and `register_feedback` / `register_operations`
   expose a feedback op. The module-level `agent_engine` object is the deploy
   entrypoint. Artifact service is GCS (`LOGS_BUCKET_NAME`) or in-memory.
5. **`app/app_utils/deploy.py`** — the Click CLI behind `make deploy`. Imports
   the entrypoint object, generates `class_methods` from `register_operations`,
   and calls `client.agent_engines.create`/`update` on Vertex AI (create-or-update
   by display name), then writes `deployment_metadata.json`. Also supports
   `--agent-identity` (per-agent IAM, Preview) and `--set-secrets`.
6. **`Makefile`** — the `register-gemini-enterprise` (via `uvx
   agent-starter-pack register-gemini-enterprise`, passing `--authorization-id`)
   and `unregister-gemini-enterprise` (a `DELETE` curl against Discovery Engine)
   targets are the second half of the OAuth story — read them alongside
   `register_oauth.py`.

### Agent (thin layer — read last)
7. **`app/agent.py`** — the runtime. A single `Agent` (`root_agent`) on Gemini
   (`MODEL_NAME` env var) with one tool, `read_drive_file`, and an instruction
   telling it to ask for a file id and to explain the "pending" auth state.
   `App(root_agent=…, name="app")` is exported for both `adk web` and deployment.

## Data handling

- **No datastore.** The only "data" is whatever Drive file the user names; there
  is no ingestion, indexing, or embedding.
- **Token handling is the real data path.** Credentials live in
  `tool_context.state` keyed by `TOKEN_CACHE_KEY`/`AUTH_ID`. Locally the full
  credential dict is cached (and refreshed via the refresh token when expired);
  in production Gemini Enterprise injects a raw access-token string under
  `temp:<AUTH_ID>`. Nothing is persisted outside session state.
- **Scope:** read-only Drive (`https://www.googleapis.com/auth/drive.readonly`),
  defined once in `auths.py` `SCOPES` and defaulted again in `register_oauth.py`.
- **File reads:** Google Docs → `text/plain`, Sheets → `text/csv`, Slides →
  `text/plain` (all via `files().export`); everything else via
  `files().get_media`. Bytes are decoded as UTF-8.

## Gotchas / things to know

- **`AUTH_ID` must match everywhere.** `TOKEN_CACHE_KEY` in `auths.py`, the
  `AUTH_ID` you pass to `make register-oauth`, and the authorization id inside
  `AUTH_ID_RESOURCE` for `make register-gemini-enterprise` must all agree, or
  Stage 1 will never find the injected `temp:<AUTH_ID>` token in production.
- **`register-gemini-enterprise` must be given `AUTH_ID_RESOURCE`** — the *full*
  resource name from `register-oauth` output
  (`projects/<num>/locations/<loc>/authorizations/<id>`), not the bare id. Omit
  it and the agent registers **without** `authorizationConfig` and production
  gets `No OAuth token available`. Verify with the `curl` in the README.
- **The Makefile `deploy` and `eval` targets reference an `adk_ae_oauth`
  package** (e.g. `--source-packages=./adk_ae_oauth`, `adk eval ./adk_ae_oauth`),
  but the agent code actually lives under **`app/`** (see `pyproject.toml`
  `module-name = "app"` and `deploy.py`'s own `./app` defaults). Adjust those
  targets to `app` if they fail — this is a leftover from the template's project
  name.
- **Local dev needs a real OAuth client.** `OAUTH_CLIENT_ID` /
  `OAUTH_CLIENT_SECRET` must be set (in `app/.env`) and
  `http://localhost:8501/dev-ui/` must be an authorized redirect URI, or the ADK
  Web UI consent screen (Stage 3) won't work.
- **`GOOGLE_CLOUD_LOCATION=global`.** `agent.py` forces the location to `global`
  at import and resolves the project from `google.auth.default()`, so imports
  need ADC. Tests import lazily / patch `google.auth.default` to avoid this.
- **Integration tests need `INTEGRATION_TEST`.** `conftest.py` ignores
  `tests/integration/*` unless `INTEGRATION_TEST` is set (they import the agent
  package at module level and hit live GCP). `make test` runs `tests/unit` then
  `tests/integration`.
- **First-time Discovery Engine setup.** `register-oauth` fails with a
  `TenantProject … global does not exist (404)` until Vertex AI Search /
  Conversation has been initialised once in the project (create any dummy Search
  or Chat app).

## Where to run things

`Makefile` targets:

- `make install` — `uv sync` (installs deps, bootstraps `uv` if missing).
- `make playground` — local ADK Web UI on port 8501 (`adk web . --reload_agents`).
- `make deploy` (alias `make backend`) — deploy to Agent Runtime via
  `app_utils/deploy.py`; writes `deployment_metadata.json`.
- `make test` — `uv run pytest tests/unit` then `tests/integration`.
- `make eval` / `make eval-all` — `adk eval` against
  `tests/eval/evalsets/*.evalset.json` with `tests/eval/eval_config.json`.
- `make lint` — `codespell` + `ruff check` + `ruff format --check` + `ty check`.
- `make register-oauth` — register the OAuth authorization resource
  (`tools/register_oauth.py`).
- `make register-gemini-enterprise` / `make unregister-gemini-enterprise` —
  link / unlink the deployed agent in Gemini Enterprise (pass `AUTH_ID_RESOURCE`,
  `GE_APP_ID`, and for unregister `AGENT_NAME`).

## Reuse (copy as-is)

- **`app/auths.py` + `app/tools.py`** are the reusable OAuth core — copy both
  into another ADK tool to get the `negotiate_creds()` three-stage pattern.
  Swap `SCOPES` and the Google API client in `read_drive_file()` for whatever
  user-scoped API you need; the credential negotiation is API-agnostic.
- **`tools/register_oauth.py`** is a self-contained script for registering an
  OAuth authorization resource with the Discovery Engine API — run it standalone
  (env vars or interactive prompts) for any Gemini Enterprise agent, not just
  this one.
- **`app/app_utils/`** (`deploy.py`, `telemetry.py`, `typing.py`) +
  `app/agent_engine_app.py` are generic Agent Runtime scaffolding — copy them to
  deploy any ADK `App` to Vertex AI Agent Engine.
- There is **no coupling to a datastore or infra**: the only external contract
  is the OAuth `AUTH_ID` (shared between `auths.py` and the two registration
  steps) and the standard `GOOGLE_CLOUD_PROJECT` / `MODEL_NAME` env vars.
