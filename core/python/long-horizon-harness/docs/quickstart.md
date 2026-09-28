# Quickstart — local-first, ~5 minutes

The lowest-friction path: tools run on your host, sessions stay in-process, and
Gemini needs no Model Garden enablement step. No Agent Platform sandbox image, no Agent
Engine, no Cloud SQL. Inference still calls Vertex (there is no local model).

> Full reference: [`configuration.md`](configuration.md) (every env var) ·
> [`extending.md`](extending.md) (lift/adapt the harness) · [`README.md`](../README.md) (overview).

## In this doc

- **Prerequisites** — uv, google-agents-cli, the gcloud SDK, and Node (web UI only).
- **GCP access (inference only)** — ADC login + the Vertex AI API.
- **Run it** — `make dev-local` (backend + web) and the local-first `.env` defaults.
- **Vertex sandbox** — the four one-time steps `make dev-sandbox` assumes you already did.
- **Smallest embeddable harness** — three env vars and one import.
- **Tests** — deterministic `pytest`, no GCP needed.
- **Connect Google (optional OAuth)** — bring your own OAuth client to wire `gcloud`/`gws` into the sandbox.

## 1. Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [google-agents-cli](https://pypi.org/project/google-agents-cli/) — `uvx google-agents-cli setup`
- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (for Vertex inference)
- Node.js 20.19+ or 22.12+ (Vite 8 requirement — only for the web UI)

## 2. GCP access (inference only)

```bash
gcloud auth application-default login   # ADC for Vertex
gcloud config set project <your-project-id>
```

Enable the **Vertex AI API**. The default `gemini-3.7-flash` needs nothing more.

## 3. Run it

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/long-horizon-harness
make dev-local          # backend (:8001, tools on host) + web UI (:3000)
```

`make dev-local` forces `LHA_ENVIRONMENT_BACKEND=local` regardless of `.env`, and
on first run bootstraps deps and seeds `.env` from [`.env.example`](../.env.example).
Open <http://localhost:3000>. One-shot from the terminal: `agents-cli run "your prompt"`.

The defaults in `.env.example` already select the local-first combo
(`LHA_ENVIRONMENT_BACKEND=local`, `USE_IN_MEMORY_SESSION=true`,
`LHA_ROOT_MODEL=gemini-3.7-flash`); `make dev` honors `.env`, `make dev-local`
ignores the backend choice in it.

**Trade-off:** no cross-session memory (sessions are in-memory, lost on restart)
and no isolated sandbox (tools execute on your machine). To get the isolated
per-user sandbox, see the next section — `make dev-sandbox` alone is not enough.

## 3b. Vertex sandbox instead of your host (optional)

> ⚠️ **`make dev-sandbox` on its own does not give you an isolated sandbox.**
> `SandboxProvider.build_environment` also needs `LHA_RUNTIME_IMAGE` and
> `LHA_SANDBOX_CALLER_SA`, both blank in `.env.example`. Off Cloud Run, if
> either is missing it logs a warning and **silently falls back to
> `LocalEnvironment` — tools keep running on your machine.** Check the backend
> log for `falling back to LocalEnvironment` before assuming you're isolated.
> (Deployed, the same condition raises `SandboxConfigurationError` instead.)

Four one-time steps:

```bash
# 1. APIs. Terraform enables these at `make deploy`, but you need them earlier
#    to build the runtime image.
gcloud services enable artifactregistry.googleapis.com cloudbuild.googleapis.com \
  aiplatform.googleapis.com --project=$PROJECT_ID

# 2. Artifact Registry repo. Nothing in terraform/ creates this one.
gcloud artifacts repositories create lha-sandbox \
  --repository-format=docker --location=us-central1 --project=$PROJECT_ID

# 3. Build + push the sandbox runtime image (Cloud Build, never local docker).
gcloud builds submit horizon/sandbox/runtime \
  --tag=us-central1-docker.pkg.dev/$PROJECT_ID/lha-sandbox/runtime:v0.1.0 \
  --project=$PROJECT_ID

# 4. A caller SA whose JWT the sandbox LB checks, and permission to mint its
#    tokens. `make deploy` creates one for Cloud Run; locally you make your own.
gcloud iam service-accounts create lha-sandbox-caller --project=$PROJECT_ID
gcloud iam service-accounts add-iam-policy-binding \
  lha-sandbox-caller@$PROJECT_ID.iam.gserviceaccount.com \
  --member="user:$(gcloud config get-value account)" \
  --role=roles/iam.serviceAccountTokenCreator --project=$PROJECT_ID
```

Then set both in `.env` and start:

```bash
LHA_RUNTIME_IMAGE=us-central1-docker.pkg.dev/<project>/lha-sandbox/runtime:v0.1.0
LHA_SANDBOX_CALLER_SA=lha-sandbox-caller@<project>.iam.gserviceaccount.com
```

```bash
make dev-sandbox   # or LHA_ENVIRONMENT_BACKEND=sandbox in .env + `make dev`
```

Lifecycle details (reattach, snapshots, version floors):
[`sandbox-lifecycle.md`](sandbox-lifecycle.md).

## 4. Smallest embeddable harness

Horizon is configured entirely by environment variable, so it embeds as a plain
ASGI app — no CLI, no Makefile. Constructs offline (no GCP credentials); inference
still calls Vertex per request (there is no local model):

```python
# my_app.py
import os

os.environ.setdefault(
    "USE_IN_MEMORY_SESSION", "true"
)  # no Cloud SQL / Agent Engine
os.environ.setdefault(
    "LHA_ENVIRONMENT_BACKEND", "local"
)  # tools run on this host
os.environ.setdefault("LHA_ROOT_MODEL", "gemini-3.7-flash")

from horizon.fast_api_app import (
    app,
)  # every router mounts; edit fast_api_app.py to trim
```

```bash
uv run uvicorn my_app:app --port 8001
```

Drive it over A2A at `http://127.0.0.1:8001/a2a`, or read `/lha/sessions`.

## 5. Tests (no GCP needed)

```bash
uv run pytest tests/unit tests/integration   # deterministic, InMemory* stand-ins
```

## 6. Connect Google (optional OAuth)

The **"Connect Google" buttons** (Auth panel in the web UI) wire each user's own
Google credentials into their sandbox — a `cloud-platform` token for `gcloud`/`bq`,
and least-privilege Workspace scopes for the `gws` CLI. The server side already
lives in the repo (`horizon/auth/oauth.py`, routes under `/lha/gcp/*`); you don't
build a webapp. What you supply is **your own OAuth client** in your own Google
Cloud project. Leave the three env vars unset and the feature is simply off
(`/connect` returns 503) — nothing else breaks.

**One-time setup (per deployment):**

1. **Consent screen** — Google Cloud Console → *APIs & Services → OAuth consent
   screen*. Choose **Internal** (users in your Workspace org only; no Google
   review) or **External** (anyone; sensitive Workspace scopes such as Gmail/Drive
   trigger Google's [app-verification](https://support.google.com/cloud/answer/13463073)).
2. **Create the client** — *Credentials → Create credentials → OAuth client ID →
   Web application*. Copy the **client ID** and **client secret**.
3. **Authorized redirect URI** — add `<your-host>/lha/gcp/callback`. For local
   dev that's `http://localhost:3000/lha/gcp/callback` (Google allows `http` only
   for `localhost`); in production it's your public web host.
4. **Enable the APIs** for the surfaces you want reachable (Drive, Gmail,
   Calendar, Sheets, Docs, Chat, Tasks, Slides, …) plus the Vertex/Cloud APIs the
   `cloud-platform` token will call.
5. **Set the env vars** (see [`configuration.md`](configuration.md#connect-google-oauth)):

   ```bash
   LHA_GCP_OAUTH_CLIENT_ID=<client-id>
   LHA_GCP_OAUTH_CLIENT_SECRET=<client-secret>   # also the HMAC key for signed state
   LHA_GCP_OAUTH_REDIRECT_URI=http://localhost:3000/lha/gcp/callback
   ```

   Tokens are stored in the per-user secret store (`LHA_SECRET_BACKEND`, default
   GCP Secret Manager; set `memory` for throwaway local testing).

Each user then clicks **Connect** in the Auth panel, consents, and their token is
injected into their sandbox commands — the model sees the secret *name*, never the
value. Tokens are **access-token only** (~1h, no refresh token requested): when one
expires the user re-clicks to renew.

> **Reused for auth.** Under `LHA_AUTH_MODE=iap`, agent-to-agent (Gemini
> Enterprise) bearer tokens are verified against this same `LHA_GCP_OAUTH_CLIENT_ID`
> (see [`security-model.md`](security-model.md)). Ignore that if you're not fronting
> the backend with Gemini Enterprise.

---

## Where to go next

- [`README.md`](../README.md) — what Long Horizon is and the full feature tour
- [`extending.md`](extending.md) — adapt without forking (skills, `scripts/<name>.py`) or lift a subsystem
- [`configuration.md`](configuration.md) — every env var and dependency extra
- [`architecture.md`](architecture.md) — architecture overview / the map
- [`commands.md`](commands.md) — slash-command catalog (`/model`, `/yolo`, …)
