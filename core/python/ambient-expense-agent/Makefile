PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION ?= us-east1
BACKEND_SERVICE ?= ambient-expense-agent
FRONTEND_SERVICE ?= expense-approval-ui
NOTIFICATION_EMAIL ?= your-email@example.com
REGISTRY = $(REGION)-docker.pkg.dev/$(PROJECT_ID)/expense-agent
BACKEND_IMAGE = $(REGISTRY)/backend
FRONTEND_IMAGE = $(REGISTRY)/frontend

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------

install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.6.12/install.sh | sh; source $$HOME/.local/bin/env; }
	uv sync

install-frontend:
	cd frontend && \
		command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.6.12/install.sh | sh; source $$HOME/.local/bin/env; } && \
		uv sync

dev:
	uv run python app/fast_api_app.py

dev-frontend:
	cd frontend && BACKEND_URL=http://localhost:8080 uv run python main.py

playground:
	uv run adk web --port 8501

test:
	uv run python -m pytest tests/ -xvs

lint:
	uv run codespell . && \
		uv run ruff check . --fix && \
		uv run ruff format . && \
		uv run mypy .

# ---------------------------------------------------------------------------
# Cloud deployment (one command)
#
#   make deploy NOTIFICATION_EMAIL=finance@example.com
#
# Builds container images, then deploys everything via Terraform:
# Cloud Run services, Pub/Sub, IAM, Cloud Monitoring.
# ---------------------------------------------------------------------------

deploy:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@if [ "$(NOTIFICATION_EMAIL)" = "your-email@example.com" ]; then \
		echo "Warning: Using default NOTIFICATION_EMAIL. Set your own:"; \
		echo "  make deploy NOTIFICATION_EMAIL=you@example.com"; \
		echo ""; \
	fi
	@echo "==> [1/4] Enabling required APIs..."
	gcloud services enable \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		--project=$(PROJECT_ID) --quiet
	@echo ""
	@echo "==> [2/4] Setting up Artifact Registry and Cloud Build permissions..."
	-gcloud artifacts repositories create expense-agent \
		--repository-format=docker \
		--location=$(REGION) \
		--project=$(PROJECT_ID) 2>/dev/null
	@PROJECT_NUMBER=$$(gcloud projects describe $(PROJECT_ID) --format='value(projectNumber)') && \
		gcloud projects add-iam-policy-binding $(PROJECT_ID) \
			--member="serviceAccount:$${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
			--role="roles/artifactregistry.writer" \
			--quiet >/dev/null && \
		gcloud projects add-iam-policy-binding $(PROJECT_ID) \
			--member="serviceAccount:$${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
			--role="roles/storage.objectViewer" \
			--quiet >/dev/null
	@echo ""
	@echo "==> [3/4] Building container images (parallel)..."
	gcloud builds submit . \
		--tag $(BACKEND_IMAGE) \
		--project=$(PROJECT_ID) --quiet & \
	gcloud builds submit frontend/ \
		--tag $(FRONTEND_IMAGE) \
		--project=$(PROJECT_ID) --quiet & \
	wait
	@echo ""
	@echo "==> [4/4] Deploying infrastructure (Cloud Run, Pub/Sub, IAM, monitoring)..."
	cd terraform && terraform init -input=false && terraform apply -auto-approve \
		-var=project_id=$(PROJECT_ID) \
		-var=region=$(REGION) \
		-var=backend_service_name=$(BACKEND_SERVICE) \
		-var=frontend_service_name=$(FRONTEND_SERVICE) \
		-var=backend_image=$(BACKEND_IMAGE) \
		-var=frontend_image=$(FRONTEND_IMAGE) \
		-var=notification_email=$(NOTIFICATION_EMAIL)
	@echo ""
	@echo "==> Deployment complete!"
	@echo ""
	@cd terraform && \
		echo "  Backend:   $$(terraform output -raw backend_url)" && \
		echo "  Frontend:  $$(terraform output -raw frontend_url)" && \
		echo "  Approval:  $$(terraform output -raw frontend_url)/approval" && \
		echo "  Topic:     expense-reports" && \
		echo "  Alerts:    Expenses >= $$100 -> $(NOTIFICATION_EMAIL)"

remote-test:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@echo "Publishing test expense (>= $$100, requires review)..."
	gcloud pubsub topics publish expense-reports \
		--project=$(PROJECT_ID) \
		--message='{"amount":250.00,"submitter":"alice@company.com","category":"travel","description":"Flight to NYC for client meeting","date":"2026-04-10"}' \
		--attribute=source=make-test
	@echo ""
	@echo "Check Cloud Logging for results:"
	@echo "  gcloud logging read 'resource.type=cloud_run_revision AND resource.labels.service_name=$(BACKEND_SERVICE)' --project=$(PROJECT_ID) --limit=5 --format='table(timestamp, textPayload)'"

clean:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set."; \
		echo "Run: gcloud config set project <your-project-id>"; \
		exit 1; \
	fi
	@echo "==> Tearing down all infrastructure..."
	cd terraform && terraform destroy -auto-approve \
		-var=project_id=$(PROJECT_ID) \
		-var=region=$(REGION) \
		-var=backend_service_name=$(BACKEND_SERVICE) \
		-var=frontend_service_name=$(FRONTEND_SERVICE) \
		-var=backend_image=$(BACKEND_IMAGE) \
		-var=frontend_image=$(FRONTEND_IMAGE) \
		-var=notification_email=$(NOTIFICATION_EMAIL) \
	|| (echo "" && \
		echo "Retrying in 60s (GCP needs time to propagate alert policy deletion)..." && \
		sleep 60 && \
		cd terraform && terraform destroy -auto-approve \
			-var=project_id=$(PROJECT_ID) \
			-var=region=$(REGION) \
			-var=backend_service_name=$(BACKEND_SERVICE) \
			-var=frontend_service_name=$(FRONTEND_SERVICE) \
			-var=backend_image=$(BACKEND_IMAGE) \
			-var=frontend_image=$(FRONTEND_IMAGE) \
			-var=notification_email=$(NOTIFICATION_EMAIL))
	@echo "Cleanup complete."
