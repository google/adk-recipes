# Environment Configuration Guide

This guide explains how to configure the customer support system using `.env` files.

## Quick Start

```bash
# 1. Copy example file
cp .env.example .env

# 2. Edit .env with your values
nano .env
```

## .env File Locations

### Root `.env` (Single source of truth)

**Location:** `/customer-support-mas/.env`

**Used by:**
- `deployment/deploy.py` - Agent Engine deployment
- `deployment/deploy-cloudrun.sh` - Cloud Run deployment
- `customer_support_mas/` - Agent system
- `backend/app/config.py` - FastAPI backend (pydantic-settings reads `../.env`)
- `ops/add_embeddings.py` - RAG setup

**Variables:**
```bash
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_CLOUD_STORAGE_BUCKET=your-bucket-name
AGENT_ENGINE_RESOURCE_NAME=projects/123/locations/us-central1/reasoningEngines/456
GOOGLE_GENAI_USE_VERTEXAI=1
FIRESTORE_DATABASE=customer-support-db
FRONTEND_URL=http://localhost:3000
PORT=8000
```

### Frontend `.env` (Optional for React frontend)

**Location:** `/customer-support-mas/frontend/.env`

**Used by:**
- Vite build process
- React environment variables (must start with `VITE_`)

**Variables:**
```bash
VITE_API_URL=http://localhost:8000
```

## Environment Variables Reference

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GOOGLE_CLOUD_PROJECT` | Your GCP project ID | `my-project-123` |
| `GOOGLE_CLOUD_STORAGE_BUCKET` | GCS bucket for staging (with `gs://` prefix) | `gs://my-bucket-staging` |
| `AGENT_ENGINE_RESOURCE_NAME` | Deployed agent resource name | `projects/123/locations/us-central1/reasoningEngines/456` |

### Optional Variables

| Variable | Description | Default | Example |
|----------|-------------|---------|---------|
| `GOOGLE_CLOUD_LOCATION` | GCP region | `us-central1` | `us-east1`, `europe-west1` |
| `FIRESTORE_DATABASE` | Firestore database name | `customer-support-db` | `(default)` |
| `AGENT_ENGINE_DISPLAY_NAME` | Display name for find-or-create logic in `deploy.py` | `customer-support-multiagent` | `customer-support-v2` |
| `GOOGLE_GENAI_USE_VERTEXAI` | Use Vertex AI instead of direct Gemini API | `1` | `0` or `1` |
| `FRONTEND_URL` | Frontend URL for CORS | `http://localhost:3000` | `https://app.example.com` |
| `PORT` | Backend port | `8000` | `8080`, `3000` |

## How It Works

### Root .env Loading

The root `.env` file is loaded by:

1. **deployment/deploy.py**
```python
from dotenv import load_dotenv
load_dotenv()
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
```

2. **customer_support_mas/database/client.py**
```python
from dotenv import load_dotenv
load_dotenv()
FIRESTORE_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
```

### Backend .env Loading

The backend `.env` is loaded automatically by Pydantic:

```python
# backend/app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    google_cloud_project: str

    class Config:
        env_file = "../.env"  # Loads root .env (single source of truth)
```

### Frontend .env Loading

Vite automatically loads `frontend/.env` and exposes variables prefixed with `VITE_`:

```typescript
// frontend/src/config.ts
const API_URL = import.meta.env.VITE_API_URL
```

## Security Best Practices

**Do:**
- Use `.env` files for local development
- Keep `.env` in `.gitignore` (already configured)
- Use environment variables or Cloud Run `--set-env-vars` for production
- Share `.env.example` files (without sensitive values)
- Rotate credentials regularly

**Do not:**
- Commit `.env` files to git
- Share `.env` files publicly
- Include credentials in `.env.example`
- Use production credentials in development

## Production Deployment

For production, use environment variables directly instead of `.env` files:

### Cloud Run

```bash
# Set environment variables in Cloud Run
gcloud run services update customer-support-backend \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=your-project-id" \
  --set-env-vars="AGENT_ENGINE_RESOURCE_NAME=projects/..." \
  --region=us-central1
```

### Vertex AI Agent Engine

Agent Engine automatically has access to:
- `GOOGLE_CLOUD_PROJECT` - Auto-detected from deployment
- `GOOGLE_CLOUD_LOCATION` - Auto-detected from deployment

## Example Workflows

### Development Workflow

```bash
# 1. Setup .env
cp .env.example .env
nano .env  # Edit with your values

# 2. Run locally
make test-local

# 3. Deploy to Agent Engine
make deploy-agent-engine

# 4. Deploy Cloud Run (frontend + backend)
make deploy-cloud-run
```

### CI/CD Workflow

CI/CD runs on Google Cloud Build. The following variables are read from `.env` by `make submit-build` and forwarded as Cloud Build substitutions:

| `.env` variable | Cloud Build substitution |
|-----------------|--------------------------|
| `GOOGLE_CLOUD_STORAGE_BUCKET` | `_STAGING_BUCKET` |
| `AGENT_ENGINE_RESOURCE_NAME` | `_AGENT_ENGINE_RESOURCE_NAME` |
| `AGENT_ENGINE_DISPLAY_NAME` | `_AGENT_ENGINE_DISPLAY_NAME` |

```bash
# Submit the full CI+CD pipeline manually
make submit-build

# Also redeploy the Agent Engine (when agent code changed)
make submit-build DEPLOY_AGENT_ENGINE=true
```

`AGENT_ENGINE_DISPLAY_NAME` controls whether `deploy.py` updates the existing engine or creates a new one. Change it in `.env` before running with `DEPLOY_AGENT_ENGINE=true` to provision a fresh engine.

See [CI_CD.md](./CI_CD.md) for full pipeline documentation.

## See Also

- [DEPLOYMENT.md](./DEPLOYMENT.md) - Full deployment guide
- [ARCHITECTURE.md](./ARCHITECTURE.md) - System architecture
- [README.md](../README.md) - Main documentation
