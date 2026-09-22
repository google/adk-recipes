# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# --- Small Business Loan Agent Hooks ---
# These targets inject setup into the Agent Starter Pack lifecycle
# Overrides default ASP backend target to orchestrate setup and deployment sequentially.

.PHONY: backend setup-env enable-services create-firestore setup-gcs deploy grant-permissions

PROJECT_ID ?= $(shell gcloud config get-value project)
BUCKET_NAME ?= $(PROJECT_ID)-small-business-loan-data

backend: setup-env enable-services create-firestore setup-gcs deploy grant-permissions
	@echo "==============================================================================="
	@echo "| Setup & Deployment Complete (Backend)                                       |"
	@echo "==============================================================================="

setup-env:
	@echo "==============================================================================="
	@echo "| Setting up Environment                                                      |"
	@echo "==============================================================================="
	@touch .env
	@if ! grep -q "^GOOGLE_CLOUD_PROJECT=" .env; then \
		PROJECT_ID=$$(gcloud config get-value project); \
		echo "GOOGLE_CLOUD_PROJECT=$$PROJECT_ID" >> .env; \
	fi
	@if ! grep -q "^GOOGLE_CLOUD_LOCATION=" .env; then \
		echo "GOOGLE_CLOUD_LOCATION=us-east1" >> .env; \
	fi

enable-services:
	@echo "==============================================================================="
	@echo "| Enabling Required Services                                                  |"
	@echo "==============================================================================="
	gcloud services enable \
		aiplatform.googleapis.com \
		firestore.googleapis.com \
		storage.googleapis.com \
		cloudresourcemanager.googleapis.com


create-firestore:
	@echo "==============================================================================="
	@echo "| Creating Firestore Database                                                 |"
	@echo "==============================================================================="
	gcloud firestore databases create \
		--database=small-business-loan-states \
		--location=nam5 \
		--type=firestore-native || echo "Database might already exist."

setup-gcs:
	@echo "==============================================================================="
	@echo "| Setting up GCS Bucket & Uploading Files                                     |"
	@echo "==============================================================================="
	gcloud storage buckets create gs://$(BUCKET_NAME) --location=us-east1 || echo "Bucket might already exist."
	gcloud storage cp data/sample_applications/*.pdf gs://$(BUCKET_NAME)/

deploy:
	# Export dependencies to requirements file using uv export.
	(uv export --no-hashes --no-header --no-dev --no-emit-project --no-annotate > small_business_loan_agent/app_utils/.requirements.txt 2>/dev/null || \
	uv export --no-hashes --no-header --no-dev --no-emit-project > small_business_loan_agent/app_utils/.requirements.txt) && \
	uv run -m small_business_loan_agent.app_utils.deploy \
		--source-packages=./small_business_loan_agent \
		--entrypoint-module=small_business_loan_agent.agent_engine_app \
		--entrypoint-object=agent_engine \
		--requirements-file=small_business_loan_agent/app_utils/.requirements.txt \
		--set-env-vars="GCS_DATA_BUCKET=$(BUCKET_NAME)" \
		$(if $(AGENT_IDENTITY),--agent-identity) \
		$(if $(filter command line,$(origin SECRETS)),--set-secrets="$(SECRETS)")
grant-permissions:
	@echo "==============================================================================="
	@echo "| Granting Permissions to Service Account of Agent Engine                                    |"
	@echo "==============================================================================="
	@PROJECT_ID=$$(gcloud config get-value project); \
	PROJECT_NUMBER=$$(gcloud projects describe $$PROJECT_ID --format='value(projectNumber)'); \
	gcloud projects add-iam-policy-binding $$PROJECT_ID \
		--member="serviceAccount:service-$$PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
		--role="roles/datastore.owner" \
		--condition=None; \
	gcloud projects add-iam-policy-binding $$PROJECT_ID \
		--member="serviceAccount:service-$$PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
		--role="roles/storage.objectViewer" \
		--condition=None