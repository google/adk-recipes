# Retail Product Search Agent

Standalone deployable ADK agent for semantic e-commerce catalog search using **Vector Search 2.0 on Gemini Enterprise Agent Platform**, **BigQuery**, and **Gemini (`gemini-3.5-flash`)**.

> **Looking for the conversational skill plugin (`SKILL.md` + BigQuery/Vector Search catalog ingestion scripts)?**
> See [`plugins/retail/skills/product-search`](../../../plugins/retail/skills/product-search/README.md).

## Agent Surfacing Personas & Boolean Config

This recipe provides boolean configuration flags in [`.env.example`](.env.example), [`app/config.py`](app/config.py), and [`Makefile`](Makefile) so you can choose which agent platforms to deploy or run:

| Persona / Platform | Boolean Flag | Default | What It Does |
|---|---|---|---|
| **Gemini Enterprise Agent Platform (Agent Engine)** | `DEPLOY_AGENT_ENGINE` | `true` | Deploys `app.agent:root_agent` as a managed ADK Reasoning Engine in Google Cloud |
| **Gemini Enterprise (GE) App (Enterprise Assistant)** | `PUBLISH_GEMINI_ENTERPRISE` | `true` | Registers the deployed Agent Engine (or A2A endpoint) with your Gemini Enterprise App |
| **Managed Agent Garden** | `PUBLISH_AGENT_GARDEN` | `true` | Validates the Agent Garden bundle (`deployable: true` in `manifest.yaml` + `agents-cli-manifest.yaml` + `Dockerfile`) and outputs Console Agent Garden links & `agents-cli` command |
| **Cloud Run (FastAPI + A2A + Reasoning Engine HTTP)** | `DEPLOY_CLOUD_RUN` | `false` | Builds the `Dockerfile` and deploys the containerized A2A/ADK server to Cloud Run |
| **Local ADK Web & A2A Server** | `RUN_LOCAL_WEB` | `false` | Starts the local FastAPI + ADK Web + A2A server at `http://127.0.0.1:8080` |

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) with Application Default Credentials (`gcloud auth application-default login`)
- A Google Cloud project with Vector Search 2.0 collection `retail-skill-products-collection` provisioned (run `make -C plugins/retail/skills/product-search setup PROJECT_ID=your-gcp-project-id` if not yet ingested).

## Setup

```bash
cd contrib/python/retail-product-search
cp .env.example .env
# Edit .env and set GOOGLE_CLOUD_PROJECT=your-gcp-project-id
uv sync
```

## Run & Deploy Each Persona

### Option A: Use Boolean Config (`make surface`)

Toggle any combination of surfacing personas in a single command:

```bash
# Deploy to Agent Engine + register in Gemini Enterprise App + validate Agent Garden:
make surface \
  PROJECT_ID=your-gcp-project-id \
  DEPLOY_AGENT_ENGINE=true \
  PUBLISH_GEMINI_ENTERPRISE=true \
  PUBLISH_AGENT_GARDEN=true \
  DEPLOY_CLOUD_RUN=false \
  RUN_LOCAL_WEB=false

# Or run only the local ADK Web + A2A server:
make surface \
  DEPLOY_AGENT_ENGINE=false \
  PUBLISH_GEMINI_ENTERPRISE=false \
  PUBLISH_AGENT_GARDEN=false \
  RUN_LOCAL_WEB=true
```

### Option B: Run Individual Personas Directly

#### 1. Local ADK Web UI & A2A Server

```bash
make run-local PORT=8080
# Or via ADK CLI:
uv run adk web . --port 8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080), select `app`, and ask:
- `"List all products in the catalog, just product_id and name."`
- `"what should I get to reduce wrist strain while working?"`
- `"setting up a home podcast studio, what do I need under $600?"`

#### 2. Deploy to Gemini Enterprise Agent Platform (Agent Engine)

```bash
make deploy-agent-engine PROJECT_ID=your-gcp-project-id REGION=us-central1
```

#### 3. Register with Gemini Enterprise (GE) App (Enterprise Search & Assistant)

```bash
make publish-gemini-enterprise \
  PROJECT_ID=your-gcp-project-id \
  GEMINI_ENTERPRISE_APP_ID="projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>" \
  AGENT_ENGINE_ID="projects/<project-number>/locations/us-central1/reasoningEngines/<reasoning-engine-id>"
```

#### 4. Deploy to Cloud Run (Containerized FastAPI + A2A)

```bash
make deploy-cloudrun PROJECT_ID=your-gcp-project-id REGION=us-central1
```

#### 5. Surface in Managed Agent Garden

```bash
make publish-agent-garden PROJECT_ID=your-gcp-project-id
```

- **Google Cloud Console (Agent Garden):** `https://console.cloud.google.com/vertex-ai/agents/agent-garden?project=your-gcp-project-id`
- **Google Cloud Console (Deployed Agent Engines):** `https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=your-gcp-project-id`
- **One-click CLI from Agent Garden catalog:** `uvx --from google-agents-cli agents-cli create retail-product-search`

## License

Apache 2.0
