
# ==============================================================================
# 1. Installation & Setup
# ==============================================================================

install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.8.13/install.sh | sh; . $$HOME/.local/bin/env; }
	uv sync

# ==============================================================================
# 2. Infrastructure (Terraform) — all variables sourced from .env
# ==============================================================================

define TF_VARS
. ./.env && \
export TF_VAR_project_id=$${GOOGLE_CLOUD_PROJECT} && \
export TF_VAR_project_name=$${PROJECT_NAME:-multiformat-hybrid-rag} && \
export TF_VAR_region=$${GOOGLE_CLOUD_LOCATION:-us-central1} && \
export TF_VAR_model_location=$${GOOGLE_CLOUD_LOCATION_MODELS:-global} && \
export TF_VAR_gcs_bucket=$${GCS_BUCKET} && \
export TF_VAR_gcs_prefix=$${GCS_PREFIX:-documents/} && \
export TF_VAR_bq_dataset=$${BQ_DATASET:-rag_pipeline} && \
export TF_VAR_bq_object_table=$${BQ_OBJECT_TABLE:-gcs_objects} && \
export TF_VAR_bq_preprocessed_table=$${BQ_PREPROCESSED_TABLE:-preprocessed} && \
export TF_VAR_bq_chunks_table=$${BQ_CHUNKS_TABLE:-chunks} && \
export TF_VAR_bq_gcs_connection_id=$${BQ_GCS_CONNECTION_ID:-rag-gcs-connection} && \
export TF_VAR_vs_collection_id=$${VS_COLLECTION_ID:-multiformat-hybrid-rag-collection} && \
export TF_VAR_vs_documents_collection_id=$${VS_DOCUMENTS_COLLECTION_ID:-multiformat-hybrid-rag-documents} && \
export TF_VAR_markdown_converter_gemini_model=$${MARKDOWN_CONVERTER_GEMINI_MODEL:-gemini-3.7-flash} && \
export TF_VAR_relevance_gemini_model=$${RELEVANCE_GEMINI_MODEL:-gemini-3.5-flash-lite} && \
export TF_VAR_contextual_chunking_gemini_model=$${CONTEXTUAL_CHUNKING_GEMINI_MODEL:-gemini-3.5-flash-lite}
endef

setup-infra:
	@if [ ! -f ./.env ]; then echo "Error: .env not found"; exit 1; fi
	@. ./.env && \
		echo "Setting project..." && \
		gcloud config set project $$GOOGLE_CLOUD_PROJECT --quiet && \
		gcloud auth application-default set-quota-project $$GOOGLE_CLOUD_PROJECT && \
		echo "Enabling bootstrap APIs..." && \
		gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
			--project=$$GOOGLE_CLOUD_PROJECT --quiet
	@echo "Provisioning infrastructure (APIs, BQ, VS2, Cloud Run, IAM)..."
	@$(TF_VARS) && cd infra/terraform/dev && terraform init && terraform apply -auto-approve
	@echo "Ensuring the Cloud Build staging bucket exists..."
	@# On a brand-new project the bucket does not exist yet. All three builds
	@# below start concurrently and each tries to auto-create it, so two of
	@# them lose the race and fail with "bucket does not exist". Creating it
	@# up front makes the parallel submit deterministic.
	@. ./.env && \
		gcloud storage buckets describe "gs://$${GOOGLE_CLOUD_PROJECT}_cloudbuild" --project="$$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1 || \
		gcloud storage buckets create "gs://$${GOOGLE_CLOUD_PROJECT}_cloudbuild" \
			--project="$$GOOGLE_CLOUD_PROJECT" --location="$${GOOGLE_CLOUD_LOCATION:-us-central1}" --quiet
	@echo "Building all images in parallel..."
	@set -a && . ./.env && set +a && { \
	gcloud builds submit --config=data_ingestion_pipeline/preprocess_service/cloudbuild.yaml \
		--substitutions=_IMAGE="$$PREPROCESS_SERVICE_IMAGE" --project="$$GOOGLE_CLOUD_PROJECT" . & \
	gcloud builds submit --config=data_ingestion_pipeline/chunk_index_service/cloudbuild.yaml \
		--substitutions=_IMAGE="$$CHUNK_INDEX_SERVICE_IMAGE" --project="$$GOOGLE_CLOUD_PROJECT" . & \
	gcloud builds submit --config=data_ingestion_pipeline/cloudbuild.yaml \
		--substitutions=_IMAGE="$$PIPELINE_IMAGE" --project="$$GOOGLE_CLOUD_PROJECT" . & \
	fail=0; for job in $$(jobs -p); do wait $$job || fail=1; done; \
	if [ $$fail -ne 0 ]; then echo "ERROR: at least one image build failed." >&2; exit 1; fi; \
	echo "All images built."; }
	@echo "Deploying Cloud Run services..."
	@set -a && . ./.env && set +a && { \
	gcloud run deploy $${PROJECT_NAME:-multiformat-hybrid-rag}-preprocess \
		--image="$$PREPROCESS_SERVICE_IMAGE" \
		--region="$$GOOGLE_CLOUD_LOCATION" \
		--project="$$GOOGLE_CLOUD_PROJECT" \
		--quiet & \
	gcloud run deploy $${PROJECT_NAME:-multiformat-hybrid-rag}-chunk-index \
		--image="$$CHUNK_INDEX_SERVICE_IMAGE" \
		--region="$$GOOGLE_CLOUD_LOCATION" \
		--project="$$GOOGLE_CLOUD_PROJECT" \
		--quiet & \
	fail=0; for job in $$(jobs -p); do wait $$job || fail=1; done; \
	if [ $$fail -ne 0 ]; then echo "ERROR: at least one service deploy failed." >&2; exit 1; fi; \
	echo "All services deployed."; }

tf-plan:
	@if [ ! -f ./.env ]; then echo "Error: .env not found"; exit 1; fi
	@$(TF_VARS) && cd infra/terraform/dev && terraform plan

tf-destroy:
	@if [ ! -f ./.env ]; then echo "Error: .env not found"; exit 1; fi
	@$(TF_VARS) && cd infra/terraform/dev && terraform destroy

# ==============================================================================
# 3. RAG Data Ingestion Pipeline
# ==============================================================================

data-ingestion-remote:
	@if [ ! -f ./.env ]; then echo "Error: .env not found"; exit 1; fi
	@. ./.env && \
	PYTHONPATH=.:data_ingestion_pipeline PIPELINE_IMAGE=$$PIPELINE_IMAGE \
	uv run python data_ingestion_pipeline/data_ingestion_pipeline/submit_pipeline.py \
		--service-account=$$SERVICE_ACCOUNT \
		--pipeline-root=$$PIPELINE_ROOT \
		--disable-caching \
		--cron-schedule="$${PIPELINE_CRON:-0 2 * * *}"


# ==============================================================================
# 4. Playground
# ==============================================================================

playground:
	@echo "==============================================================================="
	@echo "| 🚀 Starting your agent playground...                                        |"
	@echo "|                                                                             |"
	@echo "| 💡 Try asking: How to save a pandas dataframe to CSV?                       |"
	@echo "|                                                                             |"
	@echo "| 🔍 IMPORTANT: Select the 'app' folder to interact with your agent.          |"
	@echo "==============================================================================="
	uv run adk web . --port 8501 --reload_agents

# ==============================================================================
# 5. Deployment
# ==============================================================================

deploy:
	@if [ ! -f ./.env ]; then echo "Error: .env not found"; exit 1; fi
	@. ./.env && \
	gcloud beta run deploy $${PROJECT_NAME:-multiformat-hybrid-rag} \
		--source . \
		--memory "4Gi" \
		--cpu "4" \
		--project $$GOOGLE_CLOUD_PROJECT \
		--region $$GOOGLE_CLOUD_LOCATION \
		--service-account=$${PROJECT_NAME:-multiformat-hybrid-rag}-app@$$GOOGLE_CLOUD_PROJECT.iam.gserviceaccount.com \
		--no-allow-unauthenticated \
		--no-cpu-throttling \
		--labels "created-by=adk" \
		--update-build-env-vars "AGENT_VERSION=$(shell awk -F'"' '/^version = / {print $$2}' pyproject.toml || echo '0.0.0')" \
		$(if $(IAP),--iap)

# ==============================================================================
# 6. Local Development
# ==============================================================================

backend: local-backend

local-backend:
	uv run uvicorn app.fast_api_app:app --host localhost --port $(or $(PORT),8000) --reload

mcp-server:
	uv run python -m app.mcp_server --transport sse --port $(or $(MCP_PORT),8081)

# ==============================================================================
# 7. Testing & Code Quality
# ==============================================================================

test:
	uv sync --dev
	uv run pytest tests/unit && uv run pytest tests/integration

lint:
	uv sync --dev --extra lint
	uv run codespell
	uv run ruff check . --diff
	uv run ruff format . --check --diff
	uv run ty check .
