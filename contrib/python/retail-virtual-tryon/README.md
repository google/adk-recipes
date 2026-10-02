# Retail Virtual Try-On Agent

Deployable **Retail Virtual Try-On ADK Agent** supporting high-fidelity still-image try-on (`gemini-2.5-flash-image` / `gemini-2.5-pro-image`) and 8-second catwalk video generation (`veo-3.1-generate-001` Reference-to-Video) on **Gemini Enterprise Agent Platform**.

> **Companion Skill Plugin:** The reusable agent skill (`SKILL.md`, `EVAL.yaml`, pre-flight product-cutout classifier, and evaluation scripts) lives in [`plugins/retail/skills/virtual-tryon`](../../../plugins/retail/skills/virtual-tryon/README.md). This folder contains the standalone deployable ADK + A2A agent service.

---

## Architecture

```
User Photo + Product Image
    │
    ▼
┌──────────────────────────────────────────┐
│  root_agent (Gemini 3.5 Flash on ADK)    │
├──────────────────────────────────────────┤
│  1. try_on_product_image                 │
│     └─► Gemini 2.5 Flash/Pro Image       │
│         (Full-body try-on composite)     │
│  2. try_on_product_video                 │
│     └─► Veo 3.1 Reference-to-Video (R2V) │
│         (8s studio catwalk animation)    │
└──────────────────────────────────────────┘
```

---

## Prerequisites & Setup

1. **Install dependencies:**
   ```bash
   make install
   ```
2. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set `GOOGLE_CLOUD_PROJECT`, `TRYON_OUTPUT_BUCKET` (optional — defaults to local `tmp/vto-outputs/`), and the **surfacing boolean flags** for the platforms you want to target.

---

## Choosing Which Agent Personas to Surface (Boolean Config)

In `.env` (copied from `.env.example`), toggle the boolean flags for the platforms you want to deploy or run:

```dotenv
# Surfacing boolean flags (choose which targets `make surface` runs)
DEPLOY_AGENT_ENGINE=true
PUBLISH_GEMINI_ENTERPRISE=true
PUBLISH_AGENT_GARDEN=true
DEPLOY_CLOUD_RUN=false
RUN_LOCAL_WEB=false

# Required when PUBLISH_GEMINI_ENTERPRISE=true
GEMINI_ENTERPRISE_APP_ID=your-gemini-enterprise-app-id
```

Then run a single command to execute all enabled surfacing targets:

```bash
make surface
```

---

## Running Each Persona Directly

You can also run or deploy each persona individually via `make` targets:

| Persona | Platform / Surface | Boolean Flag in `.env` | Direct Command |
| :--- | :--- | :--- | :--- |
| **1. Managed Reasoning Engine** | **Gemini Enterprise Agent Platform (Agent Engine)** | `DEPLOY_AGENT_ENGINE=true` | `make deploy-agent-engine` |
| **2. Enterprise Assistant** | **Gemini Enterprise App** | `PUBLISH_GEMINI_ENTERPRISE=true` | `make register-gemini-enterprise GEMINI_ENTERPRISE_APP_ID=<app-id>` |
| **3. Managed Agent Garden** | **Google Cloud Console Agent Garden / Agents CLI** | `PUBLISH_AGENT_GARDEN=true` | `make publish-agent-garden` |
| **4. Containerized A2A Microservice** | **Cloud Run (FastAPI + A2A)** | `DEPLOY_CLOUD_RUN=true` | `make deploy` |
| **5. Interactive Dev UI** | **ADK Web Playground / Local FastAPI** | `RUN_LOCAL_WEB=true` | `make playground` or `make local-backend` |

### 1. Gemini Enterprise Agent Platform (Agent Engine)
Deploys `app/agent.py` (`root_agent`) as a managed Reasoning Engine in your GCP project:
```bash
make deploy-agent-engine PROJECT_ID=your-gcp-project-id LOCATION=us-central1
```

### 2. Gemini Enterprise App (Enterprise Search & Assistant)
Registers the deployed agent with your Gemini Enterprise application:
```bash
make register-gemini-enterprise PROJECT_ID=your-gcp-project-id GEMINI_ENTERPRISE_APP_ID=your-app-id
```

### 3. Managed Agent Garden
Validates the Agent Garden bundle (`deployable: true` in `manifest.yaml`, `agents-cli-manifest.yaml`, and `Dockerfile`) and prints the Google Cloud Console Agent Garden links & `agents-cli` command:
```bash
make publish-agent-garden PROJECT_ID=your-gcp-project-id
```
- **Google Cloud Console (Agent Garden):** `https://console.cloud.google.com/vertex-ai/agents/agent-garden?project=your-gcp-project-id`
- **Google Cloud Console (Deployed Agent Engines):** `https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=your-gcp-project-id`
- **One-click CLI from Agent Garden catalog:** `uvx --from google-agents-cli agents-cli create retail-virtual-tryon`

### 4. Cloud Run (FastAPI + A2A Server)
Builds `Dockerfile` and deploys the FastAPI + A2A service (`/.well-known/agent.json`, `/a2a`, `/feedback`, `/health`):
```bash
make deploy PROJECT_ID=your-gcp-project-id LOCATION=us-central1
```

### 5. Local Development (ADK Web Playground & FastAPI)
Launch the interactive ADK web UI:
```bash
make playground
```
Or start the local FastAPI + A2A server on port `8080`:
```bash
make local-backend
```

---

## Testing

Run the offline unit test suite:
```bash
make test
```
