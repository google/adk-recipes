SHELL := /bin/bash
PYTHONPATH = genmedia4commerce

# ==============================================================================
# Installation & Setup
# ==============================================================================

# Install dependencies using uv package manager
install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.8.13/install.sh | sh; source $HOME/.local/bin/env; }
	@command -v g++ >/dev/null 2>&1 || { echo "Installing build tools (g++)..."; apt-get update -qq && apt-get install -y -qq build-essential g++ > /dev/null 2>&1 || true; }
	uv sync

# Sync the root config.env into the package directory.
# The runtime loads genmedia4commerce/config.env (the package-internal one) because
# at deploy time only the package directory ships. The root config.env is the
# user-facing source of truth; the package one is a build artifact (gitignored).
sync-config:
	@if [ ! -f config.env ]; then echo "Error: config.env not found. Copy config.env.example to config.env and edit it."; exit 1; fi
	@cp config.env genmedia4commerce/config.env

# ==============================================================================
# Playground Targets
# ==============================================================================

# Launch local dev playground
playground: sync-config
	@echo "==============================================================================="
	@echo "| 🚀 Starting your agent playground...                                        |"
	@echo "|                                                                             |"
	@echo "| 💡 Try asking: What's the weather in San Francisco?                         |"
	@echo "|                                                                             |"
	@echo "| 🔍 IMPORTANT: Select the 'genmedia4commerce' folder to interact with your agent.          |"
	@echo "==============================================================================="
	uv run adk web . --port 8501 --reload_agents

# ==============================================================================
# Backend Deployment Targets
# ==============================================================================

# Deploy the agent remotely
# Usage: make deploy [AGENT_IDENTITY=true] [SECRETS="KEY=SECRET_ID,..."] - Set AGENT_IDENTITY=true to enable per-agent IAM identity (Preview)
deploy:
	# Export dependencies to requirements file using uv export.
	(uv export --no-hashes --no-header --no-dev --no-emit-project --no-annotate > genmedia4commerce/app_utils/.requirements.txt 2>/dev/null || \
	uv export --no-hashes --no-header --no-dev --no-emit-project > genmedia4commerce/app_utils/.requirements.txt) && \
	uv run -m genmedia4commerce.app_utils.deploy \
		--source-packages=./genmedia4commerce \
		--entrypoint-module=genmedia4commerce.agent_engine_app \
		--entrypoint-object=agent_engine \
		--requirements-file=genmedia4commerce/app_utils/.requirements.txt \
		$(if $(AGENT_IDENTITY),--agent-identity) \
		$(if $(filter command line,$(origin SECRETS)),--set-secrets="$(SECRETS)")

# Alias for 'make deploy' for backward compatibility
backend: deploy

# Deploy to Agent Engine with app-specific resource requirements
# Usage: make deploy-agent-engine [AGENT_IDENTITY=true] [SECRETS="KEY=SECRET_ID,..."]
deploy-agent-engine: sync-config
	source config.env && gcloud config set project $$PROJECT_ID --quiet && \
	(uv export --no-hashes --no-header --no-dev --no-emit-project --no-annotate > genmedia4commerce/app_utils/.requirements.txt 2>/dev/null || \
	uv export --no-hashes --no-header --no-dev --no-emit-project > genmedia4commerce/app_utils/.requirements.txt) && \
	uv run -m genmedia4commerce.app_utils.deploy \
		--project=$${PROJECT_ID} \
		--location=$${DEFAULT_REGION:-us-central1} \
		--source-packages=./genmedia4commerce \
		--source-packages=./startup_scripts \
		--entrypoint-module=genmedia4commerce.agent_engine_app \
		--entrypoint-object=agent_engine \
		--requirements-file=genmedia4commerce/app_utils/.requirements.txt \
		--display-name=genmedia-for-commerce \
		--description="GenMedia for Commerce agent" \
		--cpu=8 \
		--memory=32Gi \
		--min-instances=2 \
		--max-instances=10 \
		--container-concurrency=7 \
		$(if $(AGENT_IDENTITY),--agent-identity) \
		$(if $(filter command line,$(origin SECRETS)),--set-secrets="$(SECRETS)")

# Deploy to Cloud Run via Cloud Build (full app: ADK agent + REST API + MCP server + frontend)
# Prerequisite: run 'make setup-infra' once to create AR repo, IAM bindings, etc.
# Usage: make deploy-cloudrun
deploy-cloudrun: sync-config
	@source config.env && gcloud config set project $$PROJECT_ID --quiet && \
	gcloud run deploy $$SERVICE_NAME \
		--source=. \
		--region=$$DEFAULT_REGION \
		--memory=$$MEMORY \
		--cpu=$$CPU \
		--timeout=$$TIMEOUT \
		--concurrency=$$CONCURRENCY \
		--min-instances=$$MIN_INSTANCES \
		--max-instances=$$MAX_INSTANCES \
		--service-account=genmedia-cloudrun@$$PROJECT_ID.iam.gserviceaccount.com \
		--no-allow-unauthenticated \
		--no-cpu-throttling \
		--labels=created-by=adk \
		--set-env-vars=PROJECT_ID=$$PROJECT_ID,DEFAULT_REGION=$$DEFAULT_REGION,COMMIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo "manual") \
		$(if $(filter true,$(USE_IAP)),--iap,)

# ==============================================================================
# Testing & Code Quality
# ==============================================================================

# Run unit and integration tests
test:
	uv sync --dev
	uv run pytest tests/unit && uv run pytest tests/integration/test_agent.py && uv run pytest tests/integration/test_agent_engine_app.py && uv run pytest tests/integration/test_server_e2e.py

# ==============================================================================
# Agent Evaluation
# ==============================================================================

# Run agent evaluation using ADK eval
# Usage: make eval [EVALSET=tests/eval/evalsets/basic.evalset.json] [EVAL_CONFIG=tests/eval/eval_config.json]
eval:
	@echo "==============================================================================="
	@echo "| Running Agent Evaluation                                                    |"
	@echo "==============================================================================="
	uv sync --dev --extra eval
	uv run adk eval ./genmedia4commerce $${EVALSET:-tests/eval/evalsets/basic.evalset.json} \
		$(if $(EVAL_CONFIG),--config_file_path=$(EVAL_CONFIG),$(if $(wildcard tests/eval/eval_config.json),--config_file_path=tests/eval/eval_config.json,))

# Run evaluation with all evalsets
eval-all:
	@echo "==============================================================================="
	@echo "| Running All Evalsets                                                        |"
	@echo "==============================================================================="
	@for evalset in tests/eval/evalsets/*.evalset.json; do \
		echo ""; \
		echo "▶ Running: $$evalset"; \
		$(MAKE) eval EVALSET=$$evalset || exit 1; \
	done
	@echo ""
	@echo "✅ All evalsets completed"

# Run code quality checks (codespell, ruff, ty)
lint:
	uv sync --dev --extra lint
	uv run codespell
	uv run ruff check . --diff
	uv run ruff format . --check --diff
	uv run ty check .

# ==============================================================================
# Gemini Enterprise Integration
# ==============================================================================

# Register the deployed agent to Gemini Enterprise
# Usage: make register-gemini-enterprise (interactive - will prompt for required details)
# For non-interactive use, set env vars: ID or GEMINI_ENTERPRISE_APP_ID (full GE resource name)
# Optional env vars: GEMINI_DISPLAY_NAME, GEMINI_DESCRIPTION, GEMINI_TOOL_DESCRIPTION, AGENT_ENGINE_ID
register-gemini-enterprise:
	@uvx agent-starter-pack@0.41.0 register-gemini-enterprise

# ==============================================================================
# Local Development
# ==============================================================================

# Run backend + frontend together (MCP server is embedded in backend)
dev:
	make run-backend & make run-frontend & wait

# Run the combined FastAPI server locally (ADK agent API + REST endpoints + MCP server + frontend)
run-backend: sync-config
	MCP_SERVER_URL=http://localhost:8081/sse PYTHONPATH=$(PYTHONPATH) uv run uvicorn genmedia4commerce.fast_api_app:app --host 0.0.0.0 --port 8000 --reload

# Build and run frontend dev server (target named to avoid conflict with frontend/ directory)
run-frontend:
	@cd frontend && npm install && npm run build && npm run dev

# Run the MCP server over HTTP/SSE (for external agents/clients)
mcp-server: sync-config
	PYTHONPATH=$(PYTHONPATH) uv run python -m mcp_server.server --transport sse --port 8081

# ==============================================================================
# Shoe Classifier (Fine-tuning & Evaluation)
# ==============================================================================

# Fine-tune Gemini LoRA for shoe side classification
train-shoe-model: sync-config
	PYTHONPATH=$(PYTHONPATH) uv run python infra/model_training/finetune_gemini_lora.py

# Evaluate all shoe classifier endpoints and update config.env with the best one
eval-set-shoe-model: sync-config
	PYTHONPATH=$(PYTHONPATH) uv run python infra/model_training/eval_gemini_lora.py

# ==============================================================================
# Infrastructure (Terraform)
# ==============================================================================

# Helper to export config.env as TF_VAR_ environment variables
define TF_VARS
source config.env && \
export TF_VAR_project_id=$${PROJECT_ID} && \
export TF_VAR_region=$${DEFAULT_REGION:-us-central1} && \
export TF_VAR_service_name=$${SERVICE_NAME:-genmedia-for-commerce} && \
export TF_VAR_memory=$${MEMORY:-32Gi} && \
export TF_VAR_cpu=$${CPU:-8} && \
export TF_VAR_timeout=$${TIMEOUT:-3600} && \
export TF_VAR_concurrency=$${CONCURRENCY:-7} && \
export TF_VAR_min_instances=$${MIN_INSTANCES:-2} && \
export TF_VAR_max_instances=$${MAX_INSTANCES:-10}
endef

# Download assets from GCS (no-op if already on disk)
pull-assets:
	@uv run python -c "from genmedia4commerce.config import pull_assets"

# One-time setup: creates AR repo, Cloud Run service, IAM bindings
setup-infra: pull-assets
	@echo "Setting up infrastructure..."
	@if [ ! -f config.env ]; then echo "Error: config.env not found"; exit 1; fi
	@source config.env && \
		gcloud config set project $$PROJECT_ID --quiet && \
		gcloud services enable cloudresourcemanager.googleapis.com --quiet
	@$(TF_VARS) && cd infra/terraform && terraform init && terraform apply

# Preview infrastructure changes
tf-plan:
	@echo "Planning Terraform changes..."
	@if [ ! -f config.env ]; then echo "Error: config.env not found"; exit 1; fi
	@$(TF_VARS) && cd infra/terraform && terraform plan

# Destroy infrastructure
tf-destroy:
	@echo "Destroying Terraform resources..."
	@if [ ! -f config.env ]; then echo "Error: config.env not found"; exit 1; fi
	@$(TF_VARS) && cd infra/terraform && terraform destroy
