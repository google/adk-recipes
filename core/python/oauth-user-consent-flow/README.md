# ADK Agent Runtime + OAuth — Google Drive Reader

A production-ready ADK agent deployed on **Agent Runtime** with **OAuth 2.0** support for reading Google Drive files on behalf of authenticated users. Works both locally via **ADK Web UI** and in production via **Gemini Enterprise**.

Built with [Google Agents CLI](https://github.com/google/agents-cli) (`adk` template, `agent_runtime` deployment target).

> **📣 Acknowledgment:** The OAuth implementation pattern in this project — including the `negotiate_creds()` three-stage credential resolution, the `auths.py` configuration, and the overall approach to making ADK + OAuth + Gemini Enterprise work together — is heavily inspired by Médéric Hurier's excellent article: **[Powering Up your Agent in Production with ADK, OAuth and Gemini Enterprise](https://fmind.medium.com/powering-up-your-agent-in-production-with-adk-oauth-and-gemini-enterprise-a52b0716fcba)**. That article was the key reference that made this implementation possible.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [How OAuth Works](#how-oauth-works)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Local Development](#local-development)
- [Production Deployment](#production-deployment)
- [Makefile Reference](#makefile-reference)
- [Troubleshooting](#troubleshooting)
- [References](#references)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        TWO MODES OF OPERATION                   │
├────────────────────────────┬────────────────────────────────────┤
│     LOCAL DEVELOPMENT      │         PRODUCTION                 │
│                            │                                    │
│  You (browser)             │  User (browser)                    │
│    ↓                       │    ↓                               │
│  ADK Web UI (:8501)        │  Gemini Enterprise UI              │
│    ↓                       │    ↓                               │
│  ADK OAuth Flow            │  Gemini Enterprise OAuth Flow      │
│  (uses auths.py config)    │  (uses registered auth resource)   │
│    ↓                       │    ↓                               │
│  negotiate_creds()         │  Token injected into               │
│  Stage 2 → Stage 3         │  tool_context.state["temp:<ID>"]   │
│    ↓                       │    ↓                               │
│  Google Drive API          │  negotiate_creds()                 │
│                            │  Stage 1 (finds injected token)    │
│                            │    ↓                               │
│                            │  Google Drive API                  │
└────────────────────────────┴────────────────────────────────────┘
```

---

## How OAuth Works

The agent needs to read files from the user's Google Drive, which requires their explicit consent via OAuth 2.0. The implementation handles **two different OAuth flows** depending on the environment:

### Local Development (ADK Web UI)

When running `make playground`, the ADK framework handles the OAuth flow:

1. User asks the agent to read a Drive file
2. `negotiate_creds()` finds no cached token → calls `tool_context.request_credential()` (Stage 3)
3. ADK Web UI redirects the user to Google's consent screen (using `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` from `app/auths.py`)
4. User grants `drive.readonly` access → ADK exchanges the auth code for tokens
5. Next tool call: `negotiate_creds()` picks up the token via `tool_context.get_auth_response()` (Stage 2)
6. Credentials are cached in `tool_context.state` for subsequent calls

**Key point:** In local dev, `auths.py` must contain valid `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` (set via environment variables or `app/.env`).

### Production (Agent Runtime + Gemini Enterprise)

When deployed to Agent Runtime and registered with Gemini Enterprise:

1. User asks the agent to read a Drive file in the Gemini Enterprise web UI
2. Gemini Enterprise sees the agent has an `authorizationConfig` with `toolAuthorizations` pointing to the registered OAuth resource
3. Gemini Enterprise prompts the user for OAuth consent using the credentials stored in the authorization resource (registered via `make register-oauth`)
4. After consent, Gemini Enterprise injects the access token into `tool_context.state["temp:<AUTH_ID>"]`
5. `negotiate_creds()` finds the injected token immediately (Stage 1) — never reaches Stage 2 or 3
6. The tool calls the Google Drive API with the injected token

**Key point:** In production, the `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` in `auths.py` are **never used**. The credentials live in the authorization resource registered via `tools/register_oauth.py`. The code only needs the `TOKEN_CACHE_KEY` (which equals `AUTH_ID`) to know where to find the injected token.

### The `negotiate_creds()` Three-Stage Pattern

This pattern (from [fmind's article](https://fmind.medium.com/powering-up-your-agent-in-production-with-adk-oauth-and-gemini-enterprise-a52b0716fcba)) makes the same code work in both environments:

| Stage | What It Does | When It's Used |
|-------|-------------|----------------|
| **Stage 1** | Check `tool_context.state` for cached or injected token | **Production**: Gemini Enterprise injects token here. **Local**: cached from previous calls |
| **Stage 2** | Check `tool_context.get_auth_response()` for completed OAuth exchange | **Local only**: ADK Web UI returns the exchanged token here after user consent |
| **Stage 3** | Call `tool_context.request_credential()` to initiate OAuth flow | **Local only**: triggers the consent screen in ADK Web UI |

---

## Project Structure

```
adk-ae-oauth/
├── app/                          # Agent code (deployed to Agent Engine)
│   ├── __init__.py               # Exports the app
│   ├── agent.py                  # Root agent definition with instruction + tools
│   ├── auths.py                  # OAuth config (scheme, credential, AUTH_CONFIG)
│   ├── tools.py                  # negotiate_creds() + read_drive_file() tool
│   ├── agent_engine_app.py       # Agent Engine wrapper (AdkApp subclass)
│   ├── app_utils/                # Deployment and telemetry utilities
│   │   ├── deploy.py             # Deployment script for Agent Engine
│   │   ├── telemetry.py          # OpenTelemetry setup
│   │   └── typing.py             # Feedback model
│   └── .env                      # Local env vars (not committed)
├── tools/                        # Standalone scripts (not deployed)
│   └── register_oauth.py         # Register OAuth auth resource with Gemini Enterprise
├── tests/                        # Unit, integration, and eval tests
├── deployment_metadata.json      # Stores deployed Agent Engine ID
├── Makefile                      # All commands
├── pyproject.toml                # Dependencies and config
└── README.md                     # This file
```

### Key Files Explained

| File | Role |
|------|------|
| `app/agent.py` | Defines the `root_agent` with `gemini-3.5-flash` model and the `read_drive_file` tool. Includes instructions on how the agent should interact with users. |
| `app/auths.py` | OAuth 2.0 configuration. Defines `AUTH_SCHEME`, `AUTH_CREDENTIAL`, and `AUTH_CONFIG` used by ADK's OAuth flow during local development. Also defines `TOKEN_CACHE_KEY` and `SCOPES` used by `negotiate_creds()` in both environments. |
| `app/tools.py` | Contains `negotiate_creds()` (the 3-stage OAuth resolution) and `read_drive_file()` (reads Google Docs, Sheets, Slides via export, and regular files via download). |
| `tools/register_oauth.py` | Standalone script that registers an OAuth authorization resource with the Discovery Engine API. This tells Gemini Enterprise which OAuth credentials to use when the agent needs user consent. **Not deployed with the agent** — run once during setup. |

---

## Prerequisites

1. **Google Cloud Project** with billing enabled
2. **`gcloud` CLI** authenticated (`gcloud auth login` + `gcloud auth application-default login`)
3. **`uv`** package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))
4. **Google Drive API** enabled:
   ```bash
   gcloud services enable drive.googleapis.com --project=YOUR_PROJECT_ID
   ```
5. **Vertex AI API** enabled:
   ```bash
   gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
   ```
6. **Discovery Engine API** enabled (required for Gemini Enterprise):
   ```bash
   gcloud services enable discoveryengine.googleapis.com --project=YOUR_PROJECT_ID
   ```
7. **OAuth 2.0 Client ID** configured in [Google Cloud Console](https://console.cloud.google.com/auth/clients):
   - Application type: **Web application**
   - Authorized redirect URIs:
     - `http://localhost:8501/dev-ui/` (for local ADK Web UI)
     - `https://vertexaisearch.cloud.google.com/oauth-redirect` (for Gemini Enterprise)

---

## Setup

### 1. Install Dependencies

```bash
uv sync --dev
```

### 2. Configure Environment Variables

Edit `app/.env` with your values:

```env
# Google Cloud
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
GOOGLE_GENAI_USE_VERTEXAI=True

# OAuth (for local ADK Web UI testing)
OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
OAUTH_CLIENT_SECRET=your-client-secret

# Auth ID (must match what you register via tools/register_oauth.py)
AUTH_ID=google-drive-auth
```

---

## Local Development

### Start the ADK Web UI

```bash
make playground
```

Open http://127.0.0.1:8501 and select the `app` folder.

### Test a Google Drive Read

1. Get a file ID from a Google Drive URL: `https://drive.google.com/file/d/<FILE_ID>/view`
2. Ask the agent: *"Read the file with ID `<FILE_ID>`"*
3. The agent will trigger the OAuth consent flow — click "Authorize" and grant `drive.readonly` access
4. The agent reads and displays the file content

---

## Production Deployment

### Step 1: Deploy to Agent Runtime

```bash
make deploy
```

This deploys the agent to Agent Runtime. The Agent Runtime ID is saved to `deployment_metadata.json`.

### Step 2: Register OAuth Authorization

Register your OAuth credentials with Gemini Enterprise so it knows how to handle the OAuth consent flow:

```bash
make register-oauth
```

You'll be prompted for:
- **Project ID**: your GCP project ID
- **Location**: `global`, `eu`, or `us` (must match your Gemini Enterprise app location)
- **Authorization ID**: e.g., `google-drive-auth` (must match `AUTH_ID` in `app/.env`)
- **OAuth Client ID**: your OAuth client ID
- **OAuth Client Secret**: your OAuth client secret
- **Scopes**: defaults to `https://www.googleapis.com/auth/drive.readonly`

### Step 3: Register Agent with Gemini Enterprise

Link the deployed agent to Gemini Enterprise **with the OAuth authorization**:

```bash
# Interactive
make register-gemini-enterprise

# Non-interactive (recommended for repeatability)
make register-gemini-enterprise \
  AUTH_ID_RESOURCE="projects/<PROJECT_NUMBER>/locations/<LOCATION>/authorizations/google-drive-auth" \
  GE_APP_ID="projects/<PROJECT_NUMBER>/locations/<LOCATION>/collections/default_collection/engines/<ENGINE_ID>" \
  DISPLAY_NAME="Drive Reader Agent" \
  DESCRIPTION="Reads files from Google Drive on behalf of the user" \
  TOOL_DESCRIPTION="Read content of a Google Drive file using OAuth"
```

> **Important:** The `AUTH_ID_RESOURCE` must be the **full resource name** from the `make register-oauth` output, not just the auth ID string.

### Step 4: Verify the Registration

Check that the agent is linked to the OAuth authorization:

```bash
curl -s \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: YOUR_PROJECT_ID" \
  "https://<LOCATION>-discoveryengine.googleapis.com/v1alpha/<AGENT_NAME>" \
  | python3 -m json.tool
```

You should see:
```json
"authorizationConfig": {
    "toolAuthorizations": [
        "projects/.../locations/.../authorizations/google-drive-auth"
    ]
}
```

### Updating an Existing Registration

The CLI always creates a new registration. To replace an existing one:

```bash
# 1. Delete the old registration
make unregister-gemini-enterprise \
  AGENT_NAME="projects/.../assistants/default_assistant/agents/<AGENT_ID>"

# 2. Re-register
make register-gemini-enterprise \
  AUTH_ID_RESOURCE="projects/.../authorizations/google-drive-auth" \
  GE_APP_ID="projects/.../engines/<ENGINE_ID>"
```

The `AGENT_NAME` is the full name printed in the register output (ending with `/agents/<id>`).

---

## Makefile Reference

| Command | Description |
|---------|-------------|
| `make install` | Install dependencies with `uv` |
| `make playground` | Start ADK Web UI on port 8501 |
| `make deploy` | Deploy agent to Agent Runtime |
| `make register-oauth` | Register OAuth authorization resource |
| `make register-gemini-enterprise` | Register agent with Gemini Enterprise |
| `make unregister-gemini-enterprise` | Delete an agent registration |
| `make test` | Run unit and integration tests |
| `make eval` | Run agent evaluation |
| `make lint` | Run code quality checks |

---

## Troubleshooting

### `Google Drive API has not been used in project ... or it is disabled`

Enable the Drive API:
```bash
gcloud services enable drive.googleapis.com --project=YOUR_PROJECT_ID
```

### `file_cache is only supported with oauth2client<4.0.0`

Harmless info message from the Google API client library. Can be safely ignored.

### `address already in use` on port 8501

Kill the existing process:
```bash
lsof -ti :8501 | xargs kill -9
```

### `The authorization URI must contain 'access_type=offline'`

The `authorizationUri` registered with Discovery Engine must include query parameters like `access_type=offline`. The `register_oauth.py` script handles this — do **not** pass just the base URL.

### OAuth consent screen not appearing in ADK Web UI

Make sure `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` are set in `app/.env`, and that `http://localhost:8501/dev-ui/` is in the Authorized redirect URIs of your OAuth client.

### Token not found in production (`No OAuth token available`)

Verify the agent registration includes the authorization config:
```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://<LOCATION>-discoveryengine.googleapis.com/v1alpha/<AGENT_NAME>" \
  | python3 -m json.tool | grep -A3 authorizationConfig
```

If `authorizationConfig` is missing, the agent was registered without `--authorization-id`. Unregister and re-register with the correct auth resource.

### `TenantProject` with location `global` does not exist (404)

This error occurs when trying to register an OAuth resource in a project where **Vertex AI Search and Conversation (Discovery Engine)** has not been initialized yet.
**Solution**: Go to the Google Cloud Console, navigate to **Vertex AI Search and Conversation**, and create a dummy Search or Chat app. This will trigger the creation of the internal tenant project and default collections.

### `authorizationConfig` missing after interactive registration

If the `curl` command in Step 4 does not show the `authorizationConfig` section, the agent was registered without linking the OAuth credentials.
**Solution**: This can happen if the interactive `make register-gemini-enterprise` command skips the authorization ID. Delete the registration and use the **non-interactive command** in Step 3 to explicitly pass `AUTH_ID_RESOURCE`.

### 404 Error on `/signin/` when clicking "Preview" in Console

If you get a 404 error pointing to `auth.cloud.google` when clicking the Preview button for your agent in the Gemini Enterprise console, this is likely a bug in the console's authentication routing for preview instances.
**Solution**: Your agent is likely configured correctly in the backend. Verify it using the `curl` command in Step 4.

---

### Alternative: Using Google Agents CLI

You can also use the [Google Agents CLI](https://github.com/google/agents-cli) to create a production-ready version of this agent with additional deployment options.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this recipe** (replace `my-adk-ae-oauth` with your project name):

```bash
agents-cli create my-adk-ae-oauth -a adk@adk-ae-oauth
```

The Google Agents CLI will prompt you to select deployment options and provides additional production-ready features including automated CI/CD deployment scripts.

---

## Makefile Reference


- [Powering Up your Agent with ADK, OAuth and Gemini Enterprise](https://fmind.medium.com/powering-up-your-agent-in-production-with-adk-oauth-and-gemini-enterprise-a52b0716fcba) — The pattern this implementation follows
- [Google ADK Documentation](https://google.github.io/adk-docs/) — Official ADK docs
- [Google Agents CLI](https://github.com/google/agents-cli) — Production templates for GCP agents
- [ADK Authentication](https://google.github.io/adk-docs/tools/authentication/) — OAuth flow details in ADK
