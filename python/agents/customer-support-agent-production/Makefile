# ==============================================================================
# Customer Support MAS — Makefile
# ==============================================================================
# Usage:
#   make <target>
#   make help          List all available targets
#
# Override defaults with environment variables or inline:
#   make test-unit EVAL_PROFILE=standard
#   make eval-post-deploy AGENT_ENGINE_ID=1234567890
# ==============================================================================

# ------------------------------------------------------------------------------
# Defaults (override from environment or command line)
# ------------------------------------------------------------------------------
PYTHON        ?= uv run python
PYTEST        ?= uv run pytest
EVAL_PROFILE  ?= fast
AGENT         ?= product
AGENT_ENGINE_ID ?=
DELAY         ?= 5
SUITE         ?=

PYTHON_VERSION := 3.11
PYTEST_FLAGS   := -v --tb=short

# Common env vars required by all Vertex AI / Firestore steps
COMMON_ENV := GOOGLE_GENAI_USE_VERTEXAI=True

# ------------------------------------------------------------------------------
# Phony targets
# ------------------------------------------------------------------------------
# ENV — target environment for multi-env Terraform targets (dev | staging | prod)
ENV ?= dev

.PHONY: help \
        install setup-gcp setup-firestore setup-cloud-build \
        setup-model-armor create-model-armor-template test-model-armor \
        seed-db add-embeddings vector-index \
        lint format \
        test-tools test-unit test-integration test \
        gen-evalset gen-integration-evalset \
        eval-post-deploy \
        frontend-install frontend-build frontend-dev \
        deploy-agent-engine test-local \
        deploy-cloud-run submit-build nightly \
        bootstrap-tfstate terraform-init terraform-plan terraform-apply terraform-destroy infra-up

# ==============================================================================
# HELP
# ==============================================================================

help: ## Show this help message
	@echo ""
	@echo "Customer Support MAS — available targets:"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "Examples:"
	@echo "  make test                              # run all tests (EVAL_PROFILE=fast)"
	@echo "  make test-unit EVAL_PROFILE=standard   # unit eval with standard profile"
	@echo "  make gen-evalset AGENT=order           # generate order agent eval dataset"
	@echo "  make eval-post-deploy AGENT_ENGINE_ID=1234567890"
	@echo "  make nightly                                    # run all steps (post-deploy off)"
	@echo "  make nightly RUN_INTEGRATION_TESTS=false RUN_POST_DEPLOY_EVAL=true"
	@echo ""

# ==============================================================================
# SETUP
# ==============================================================================

install: ## Install Python deps + pre-commit hooks (uses uv)
	pip install uv --quiet
	uv sync --frozen --group dev
	uv run pre-commit install
	@echo "Done. Run 'make setup-gcp' next if setting up GCP for the first time."

setup-gcp: ## Enable GCP APIs and configure IAM (reads .env)
	bash scripts/setup_gcp.sh

setup-firestore: ## Create Firestore database and seed sample data
	bash scripts/setup_firestore.sh

setup-cloud-build: ## Configure Cloud Build IAM, Artifact Registry, and Secret Manager
	@PROJECT_ID="$(PROJECT_ID)"; \
	REGION="$(REGION)"; \
	STAGING_BUCKET="$(STAGING_BUCKET)"; \
	if [ -f .env ]; then \
		if [ -z "$$PROJECT_ID" ];     then PROJECT_ID=$$(grep '^GOOGLE_CLOUD_PROJECT='           .env | cut -d= -f2-); fi; \
		if [ -z "$$REGION" ];         then REGION=$$(grep '^GOOGLE_CLOUD_LOCATION='              .env | cut -d= -f2-); fi; \
		if [ -z "$$STAGING_BUCKET" ]; then STAGING_BUCKET=$$(grep '^GOOGLE_CLOUD_STORAGE_BUCKET=' .env | cut -d= -f2- | sed 's|gs://||'); fi; \
	fi; \
	if [ -z "$$PROJECT_ID" ] || [ -z "$$REGION" ] || [ -z "$$STAGING_BUCKET" ]; then \
		echo "Error: PROJECT_ID, REGION, and STAGING_BUCKET are required."; \
		echo "Add them to .env or pass inline: make setup-cloud-build PROJECT_ID=<id> REGION=<region> STAGING_BUCKET=<bucket>"; \
		exit 1; \
	fi; \
	GITHUB_OWNER_ARG="$(GITHUB_OWNER)"; \
	if [ -z "$$GITHUB_OWNER_ARG" ] && [ -f .env ]; then \
		GITHUB_OWNER_ARG=$$(grep '^GITHUB_OWNER=' .env | cut -d= -f2- || echo ""); \
	fi; \
	bash scripts/setup-cloud-build.sh "$$PROJECT_ID" "$$REGION" "$$STAGING_BUCKET" "$$GITHUB_OWNER_ARG"

setup-model-armor: ## Enable Model Armor and configure floor settings
	@ARGS=""; \
	if [ -n "$(MODE)" ]; then ARGS="$$ARGS --mode $(MODE)"; fi; \
	if [ -n "$(CREATE_TEMPLATE)" ]; then ARGS="$$ARGS --create-template"; fi; \
	bash scripts/setup_model_armor.sh $$ARGS

create-model-armor-template: ## Create Model Armor template via Python SDK (use when gcloud model-armor is unavailable)
	PYTHONPATH=. $(PYTHON) scripts/create_model_armor_template.py

test-model-armor: ## Smoke test Model Armor API (safe + unsafe prompts)
	PYTHONPATH=. $(PYTHON) scripts/test_model_armor.py

seed-db: ## Seed Firestore with sample products, orders, invoices, users (use ENV=staging|prod)
	$(eval ENV_FILE := $(if $(ENV),.env.$(ENV),.env))
	set -a && . ./$(ENV_FILE) && set +a && PYTHONPATH=. $(PYTHON) customer_support_mas/database/fixtures.py \
		--project $(shell grep GOOGLE_CLOUD_PROJECT $(if $(ENV),.env.$(ENV),.env) | cut -d= -f2) \
		--database $(shell grep FIRESTORE_DATABASE $(if $(ENV),.env.$(ENV),.env) | cut -d= -f2 || echo customer-support-db)

add-embeddings: ## Add vector embeddings to Firestore products (use ENV=staging|prod)
	$(eval ENV_FILE := $(if $(ENV),.env.$(ENV),.env))
	set -a && . ./$(ENV_FILE) && set +a && PYTHONPATH=. $(PYTHON) ops/add_embeddings.py \
		--project $(shell grep GOOGLE_CLOUD_PROJECT $(if $(ENV),.env.$(ENV),.env) | cut -d= -f2) \
		--database $(shell grep FIRESTORE_DATABASE $(if $(ENV),.env.$(ENV),.env) | cut -d= -f2 || echo customer-support-db) \
		--location $(shell grep GOOGLE_CLOUD_LOCATION $(if $(ENV),.env.$(ENV),.env) | cut -d= -f2 || echo us-central1)

vector-index: ## Create Firestore vector index for semantic search (use ENV=staging|prod)
	$(eval ENV_FILE := $(if $(ENV),.env.$(ENV),.env))
	set -a && . ./$(ENV_FILE) && set +a && PYTHONPATH=. $(PYTHON) ops/create_vector_index.py

# ==============================================================================
# LINT & FORMAT
# ==============================================================================

lint: ## Check code style (ruff check + ruff format --check)
	ruff check customer_support_agent/ --ignore=E501
	ruff format customer_support_agent/ --check

format: ## Auto-fix formatting with ruff
	ruff format customer_support_agent/
	ruff check customer_support_agent/ --fix --ignore=E501

# ==============================================================================
# TESTS
# ==============================================================================

test-tools: ## Run pure tool tests (no LLM, mocked Firestore) — fast
	$(COMMON_ENV) $(PYTEST) \
		tests/unit/test_tools.py \
		tests/unit/test_mock_rag.py \
		tests/unit/test_refund_standalone.py \
		$(PYTEST_FLAGS)

test-unit: ## Run unit agent eval (EVAL_PROFILE=fast|standard|full)
	$(COMMON_ENV) EVAL_PROFILE=$(EVAL_PROFILE) $(PYTEST) \
		tests/unit/test_agent_eval_ci.py \
		$(PYTEST_FLAGS)

test-integration: ## Run integration eval (EVAL_PROFILE=fast|standard|full, TEST=test_name to filter)
	$(COMMON_ENV) EVAL_PROFILE=$(EVAL_PROFILE) $(PYTEST) \
		tests/integration/test_integration_eval_ci.py \
		$(if $(TEST),-k $(TEST),) \
		$(PYTEST_FLAGS)

test: test-tools test-unit test-integration ## Run all tests (EVAL_PROFILE=fast by default)

# ==============================================================================
# EVAL DATASET GENERATION
# ==============================================================================

gen-evalset: ## Generate unit eval dataset — AGENT=product|order|billing (default: product)
	@ARGS="--agent $(AGENT) --delay $(DELAY)"; \
	if [ -n "$(DRY_RUN)" ]; then ARGS="$$ARGS --dry-run"; fi; \
	PYTHONPATH=. $(PYTHON) scripts/generate_eval_dataset.py $$ARGS

gen-integration-evalset: ## Generate integration eval dataset
	@ARGS="--delay $(DELAY)"; \
	if [ -n "$(SUITE)" ]; then ARGS="$$ARGS --suite $(SUITE)"; fi; \
	if [ -n "$(DRY_RUN)" ]; then ARGS="$$ARGS --dry-run"; fi; \
	PYTHONPATH=. $(PYTHON) scripts/generate_integration_evalset.py $$ARGS

# ==============================================================================
# POST-DEPLOY EVALUATION
# ==============================================================================

eval-post-deploy: ## Evaluate deployed Agent Engine (use ENV=staging|prod; AGENT_ENGINE_ID optional)
	$(eval ENV_FILE := $(if $(ENV),.env.$(ENV),.env))
	@AGENT_ID="$(AGENT_ENGINE_RESOURCE_NAME)"; \
	if [ -z "$$AGENT_ID" ]; then AGENT_ID="$(AGENT_ENGINE_ID)"; fi; \
	if [ -z "$$AGENT_ID" ] && [ -f $(ENV_FILE) ]; then \
		AGENT_ID=$$(grep '^AGENT_ENGINE_RESOURCE_NAME=' $(ENV_FILE) | cut -d= -f2-); \
	fi; \
	if [ -z "$$AGENT_ID" ]; then \
		echo "Error: AGENT_ENGINE_ID or AGENT_ENGINE_RESOURCE_NAME is required."; \
		echo "Usage: make eval-post-deploy ENV=staging [AGENT_ENGINE_ID=<id>]"; \
		exit 1; \
	fi; \
	set -a && . ./$(ENV_FILE) && set +a && PYTHONPATH=. $(PYTHON) tests/eval_vertex.py \
		--agent-engine-id "$$AGENT_ID" \
		--profile $(if $(filter fast,$(EVAL_PROFILE)),standard,$(EVAL_PROFILE)) \
		--custom-inference \
		--delay $(DELAY)

# ==============================================================================
# FRONTEND
# ==============================================================================

# ==============================================================================
# TERRAFORM — Infrastructure as Code (multi-environment)
# ==============================================================================
# Usage:
#   make infra-up ENV=dev        # init + apply for dev
#   make infra-up ENV=staging    # init + apply for staging
#   make infra-up ENV=prod       # init + apply for prod
#   make terraform-plan ENV=dev  # preview changes only
#
# First-time per environment — create the GCS state bucket:
#   make bootstrap-tfstate ENV=dev
#
# State is stored in GCS: gs://{project_id}-tf-state/customer-support-mas/{env}/
# tfvars are stored in GCS: gs://{project_id}-tf-state/tfvars/terraform.tfvars
# (Cloud Build reads them from there; update GCS after any local tfvars change)

_TF_DIR = terraform/environments/$(ENV)
_TF_PROJECT = $(shell grep '^project_id' $(_TF_DIR)/terraform.tfvars 2>/dev/null | sed 's/.*= *"\([^"]*\)".*/\1/')
_TF_STATE_BUCKET = $(shell grep '^tfstate_bucket_name' $(_TF_DIR)/terraform.tfvars 2>/dev/null | sed 's/.*= *"\([^"]*\)".*/\1/' | grep . || echo "$(_TF_PROJECT)-tf-state")

bootstrap-tfstate: ## Create GCS state bucket + upload tfvars (once per env). Usage: make bootstrap-tfstate ENV=dev
	@[ -n "$(_TF_PROJECT)" ] || (echo "ERROR: project_id not found in $(_TF_DIR)/terraform.tfvars"; exit 1)
	@[ -f "$(_TF_DIR)/terraform.tfvars" ] || (echo "ERROR: $(_TF_DIR)/terraform.tfvars not found. Copy from terraform.tfvars.example."; exit 1)
	@echo "Creating state bucket: $(_TF_STATE_BUCKET) in project $(_TF_PROJECT)"
	gsutil mb -p $(_TF_PROJECT) -l us-central1 gs://$(_TF_STATE_BUCKET) || true
	gsutil versioning set on gs://$(_TF_STATE_BUCKET)
	gsutil uniformbucketlevelaccess set on gs://$(_TF_STATE_BUCKET)
	@echo "Uploading tfvars to GCS..."
	gsutil cp $(_TF_DIR)/terraform.tfvars gs://$(_TF_STATE_BUCKET)/tfvars/terraform.tfvars
	@echo ""
	@echo "Done. Run: make infra-up ENV=$(ENV)"

terraform-init: ## Initialize Terraform for ENV (e.g. make terraform-init ENV=dev)
	@[ -f "$(_TF_DIR)/terraform.tfvars" ] || (echo "ERROR: $(_TF_DIR)/terraform.tfvars not found."; exit 1)
	cd $(_TF_DIR) && terraform init \
		-backend-config="bucket=$(_TF_STATE_BUCKET)" \
		-backend-config="prefix=customer-support-mas/$(ENV)" \
		-input=false \
		-reconfigure

terraform-plan: ## Preview infrastructure changes for ENV (e.g. make terraform-plan ENV=dev)
	cd $(_TF_DIR) && terraform plan -var-file=terraform.tfvars -input=false

terraform-apply: ## Apply infrastructure changes for ENV (e.g. make terraform-apply ENV=dev)
	cd $(_TF_DIR) && terraform apply -var-file=terraform.tfvars -input=false

terraform-destroy: ## Destroy all infrastructure for ENV — DESTRUCTIVE (e.g. make terraform-destroy ENV=dev)
	@echo "WARNING: This will destroy ALL infrastructure for environment '$(ENV)' (project: $(_TF_PROJECT))."
	@read -p "Type '$(ENV)' to confirm: " confirm && [ "$$confirm" = "$(ENV)" ]
	cd $(_TF_DIR) && terraform destroy -var-file=terraform.tfvars -input=false

infra-up: terraform-init terraform-apply ## Init + apply Terraform for ENV. Usage: make infra-up ENV=dev

sync-tfvars: ## Upload updated local tfvars to GCS so CI picks up the changes. Usage: make sync-tfvars ENV=dev
	@[ -f "$(_TF_DIR)/terraform.tfvars" ] || (echo "ERROR: $(_TF_DIR)/terraform.tfvars not found."; exit 1)
	gsutil cp $(_TF_DIR)/terraform.tfvars gs://$(_TF_STATE_BUCKET)/tfvars/terraform.tfvars
	@echo "Uploaded $(_TF_DIR)/terraform.tfvars → gs://$(_TF_STATE_BUCKET)/tfvars/terraform.tfvars"

# ==============================================================================
# FRONTEND
# ==============================================================================

frontend-install: ## Install frontend npm dependencies
	cd frontend && npm ci

frontend-build: ## Build React frontend for production
	cd frontend && npm run build

frontend-dev: ## Start frontend dev server (hot reload)
	cd frontend && npm start

# ==============================================================================
# DEPLOYMENT
# ==============================================================================

test-local: ## Run agent locally to verify before deploying
	PYTHONPATH=. $(PYTHON) deployment/deploy.py --action test_local

deploy-agent-engine: ## Deploy agent to Vertex AI Agent Engine (use ENV=staging|prod)
	$(eval ENV_FILE := $(if $(ENV),.env.$(ENV),.env))
	set -a && . ./$(ENV_FILE) && set +a && PYTHONPATH=. $(PYTHON) deployment/deploy.py --action deploy

deploy-cloud-run: ## Build and deploy backend to Cloud Run
	bash deployment/deploy-cloudrun.sh

nightly: ## Trigger ci-manual Cloud Build with selective step flags
	@# Defaults: all steps on, post-deploy off. Override with RUN_LINT=false, RUN_UNIT_TESTS=false, etc.
	@# RUN_POST_DEPLOY_EVAL=true requires AGENT_ENGINE_ID (or AGENT_ENGINE_RESOURCE_NAME in .env)
	@PROJECT_ID=$$(grep '^GOOGLE_CLOUD_PROJECT=' .env | cut -d= -f2-); \
	STAGING_BUCKET=$$(grep '^GOOGLE_CLOUD_STORAGE_BUCKET=' .env | cut -d= -f2-); \
	RUN_LINT_VAL="$(if $(RUN_LINT),$(RUN_LINT),true)"; \
	RUN_TOOL_VAL="$(if $(RUN_TOOL_TESTS),$(RUN_TOOL_TESTS),true)"; \
	RUN_UNIT_VAL="$(if $(RUN_UNIT_TESTS),$(RUN_UNIT_TESTS),true)"; \
	RUN_INT_VAL="$(if $(RUN_INTEGRATION_TESTS),$(RUN_INTEGRATION_TESTS),true)"; \
	RUN_PD_VAL="$(if $(RUN_POST_DEPLOY_EVAL),$(RUN_POST_DEPLOY_EVAL),false)"; \
	AGENT_ID="$(AGENT_ENGINE_ID)"; \
	if [ -z "$$AGENT_ID" ] && [ -f .env ]; then \
		AGENT_ID=$$(grep '^AGENT_ENGINE_RESOURCE_NAME=' .env | cut -d= -f2-); \
	fi; \
	gcloud builds triggers run ci-manual \
		--project="$$PROJECT_ID" \
		--region=us-central1 \
		--branch=main \
		--substitutions="_RUN_LINT=$$RUN_LINT_VAL,_RUN_TOOL_TESTS=$$RUN_TOOL_VAL,_RUN_UNIT_TESTS=$$RUN_UNIT_VAL,_RUN_INTEGRATION_TESTS=$$RUN_INT_VAL,_RUN_POST_DEPLOY_EVAL=$$RUN_PD_VAL,_AGENT_ENGINE_ID=$$AGENT_ID,_STAGING_BUCKET=$$STAGING_BUCKET"

submit-build: ## Submit full CI+CD pipeline to Cloud Build (DEPLOY_AGENT_ENGINE=true to also redeploy agent)
	@PROJECT_ID=$$(grep '^GOOGLE_CLOUD_PROJECT=' .env | cut -d= -f2-); \
	STAGING_BUCKET=$$(grep '^GOOGLE_CLOUD_STORAGE_BUCKET=' .env | cut -d= -f2-); \
	AGENT_ENGINE_RESOURCE_NAME=$$(grep '^AGENT_ENGINE_RESOURCE_NAME=' .env | cut -d= -f2-); \
	AGENT_ENGINE_DISPLAY_NAME=$$(grep '^AGENT_ENGINE_DISPLAY_NAME=' .env | cut -d= -f2-); \
	COMMIT_SHA=$$(git rev-parse HEAD); \
	gcloud builds submit . \
		--config cloudbuild/cloudbuild-deploy.yaml \
		--project "$$PROJECT_ID" \
		--substitutions "COMMIT_SHA=$$COMMIT_SHA,_STAGING_BUCKET=$$STAGING_BUCKET,_AGENT_ENGINE_RESOURCE_NAME=$$AGENT_ENGINE_RESOURCE_NAME,_AGENT_ENGINE_DISPLAY_NAME=$$AGENT_ENGINE_DISPLAY_NAME,_DEPLOY_AGENT_ENGINE=$(if $(DEPLOY_AGENT_ENGINE),$(DEPLOY_AGENT_ENGINE),false),_EVAL_PROFILE=$(EVAL_PROFILE)"
