# GitHub Gemini Enterprise App

A GitHub App webhook recipe that answers issue and pull-request comments with a
Gemini agent grounded in a Gemini Enterprise datastore, also known as Agent
Platform Search / Discovery Engine.

The recipe listens for comments that start with `@gemini-enterprise`, sends the
comment text to an ADK agent with `VertexAiSearchTool`, and posts the grounded
answer back to the same GitHub thread.

## Architecture

```text
GitHub issue or PR comment
        |
        v
FastAPI webhook verifies X-Hub-Signature-256
        |
        v
GitHub App installation token
        |
        v
ADK agent + VertexAiSearchTool
        |
        v
Gemini Enterprise datastore
        |
        v
GitHub comment reply
```

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A GitHub App with:
  - **Issues: Read and write**
  - **Pull requests: Read and write**
  - **Contents: Read-only** if you want repository context in GitHub URLs
  - Webhook events: **Issue comments** and **Pull request review comments**
- A Google Cloud project with Vertex AI and Discovery Engine enabled
- A Gemini Enterprise datastore resource name or its component values:
  `DATA_STORE_REGION`, `DATA_STORE_COLLECTION`, and `DATA_STORE_ID`

If you need to create a datastore first, see
`core/python/rag-agent-search/infra/terraform` for a managed GCS connector
example that provisions Agent Platform Search infrastructure.

## Install the skill

Install the conversational setup guide into a supported coding assistant:

```bash
npx skills add google/adk-samples --skill enterprise-github-gemini-enterprise-app
```

For the GitHub webhook service, clone the repository or use the installed
skill's directory, then follow the configuration and running steps below.

## Installation

```bash
cd skills/enterprise/github-gemini-enterprise-app
uv sync
cp .env.example .env
```

Fill in `.env`:

```bash
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY_PATH=/secure/path/github-app.private-key.pem
GITHUB_WEBHOOK_SECRET=your-webhook-secret
GITHUB_APP_SLUG=your-app-slug
GITHUB_COMMAND_PREFIX=@gemini-enterprise
GITHUB_API_URL=https://api.github.com

GOOGLE_GENAI_USE_VERTEXAI=True
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
DATA_STORE_REGION=global
DATA_STORE_COLLECTION=your-collection
DATA_STORE_ID=your-data-store-id
MODEL_NAME=gemini-3.5-flash
```

Authenticate to Google Cloud for local development:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GOOGLE_CLOUD_PROJECT"
```

## Running locally

```bash
bash scripts/run_local.sh
```

The service binds to `127.0.0.1` by default. For local integration testing,
expose it through an approved secure HTTPS tunnel and set the GitHub App
webhook URL to:

```text
https://<your-tunnel-host>/github/webhook
```

Then comment on an issue or pull request:

```text
@gemini-enterprise How do I configure SSO for this product?
```

The app verifies the webhook signature, runs the Gemini Enterprise-grounded
agent, and posts a reply in the thread.

Do not expose the service directly to the public internet. Keep the private key
and webhook secret out of source control and configure them through protected
environment variables or a secret manager.

## Supported events

| Event | Behavior |
| --- | --- |
| `issue_comment.created` | Replies to issue comments and pull-request conversation comments |
| `pull_request_review_comment.created` | Replies to code-review comment threads |

Comments from the app's own bot account are ignored to avoid reply loops.

## Running tests

```bash
uv run pytest tests/ -v
```

The tests set `INTEGRATION_TEST=TRUE`, so they do not call GitHub or a live
Gemini Enterprise datastore.
