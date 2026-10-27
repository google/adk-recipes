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

-include .env
export

PROJECT_ID ?= $(GOOGLE_CLOUD_PROJECT)
LOCATION ?= $(or $(GCP_REGION),us-central1)
SERVICE_NAME ?= retail-virtual-tryon
PORT ?= 8080

DEPLOY_AGENT_ENGINE ?= true
PUBLISH_GEMINI_ENTERPRISE ?= true
PUBLISH_AGENT_GARDEN ?= true
DEPLOY_CLOUD_RUN ?= false
RUN_LOCAL_WEB ?= false

install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Install it: https://docs.astral.sh/uv/"; exit 1; }
	uv sync

playground:
	uv run adk web . --port 8501 --reload_agents

local-backend:
	uv run uvicorn app.fast_api_app:app --host 0.0.0.0 --port $(PORT) --reload

surface:
	@echo "=== Surfacing Retail Virtual Try-On Agent (contrib/python/retail-virtual-tryon) ==="
	@echo "DEPLOY_AGENT_ENGINE       = $(DEPLOY_AGENT_ENGINE)"
	@echo "PUBLISH_GEMINI_ENTERPRISE = $(PUBLISH_GEMINI_ENTERPRISE)"
	@echo "PUBLISH_AGENT_GARDEN      = $(PUBLISH_AGENT_GARDEN)"
	@echo "DEPLOY_CLOUD_RUN          = $(DEPLOY_CLOUD_RUN)"
	@echo "RUN_LOCAL_WEB             = $(RUN_LOCAL_WEB)"
	@if [ "$(DEPLOY_AGENT_ENGINE)" = "true" ] || [ "$(DEPLOY_AGENT_ENGINE)" = "TRUE" ] || [ "$(DEPLOY_AGENT_ENGINE)" = "1" ]; then \
		$(MAKE) deploy-agent-engine; \
	fi
	@if [ "$(PUBLISH_GEMINI_ENTERPRISE)" = "true" ] || [ "$(PUBLISH_GEMINI_ENTERPRISE)" = "TRUE" ] || [ "$(PUBLISH_GEMINI_ENTERPRISE)" = "1" ]; then \
		if [ -n "$(GEMINI_ENTERPRISE_APP_ID)$(GE_APP_ID)" ]; then \
			$(MAKE) register-gemini-enterprise; \
		else \
			echo "Skipping register-gemini-enterprise: set GEMINI_ENTERPRISE_APP_ID in .env to publish."; \
		fi; \
	fi
	@if [ "$(PUBLISH_AGENT_GARDEN)" = "true" ] || [ "$(PUBLISH_AGENT_GARDEN)" = "TRUE" ] || [ "$(PUBLISH_AGENT_GARDEN)" = "1" ]; then \
		$(MAKE) publish-agent-garden; \
	fi
	@if [ "$(DEPLOY_CLOUD_RUN)" = "true" ] || [ "$(DEPLOY_CLOUD_RUN)" = "TRUE" ] || [ "$(DEPLOY_CLOUD_RUN)" = "1" ]; then \
		$(MAKE) deploy; \
	fi
	@if [ "$(RUN_LOCAL_WEB)" = "true" ] || [ "$(RUN_LOCAL_WEB)" = "TRUE" ] || [ "$(RUN_LOCAL_WEB)" = "1" ]; then \
		$(MAKE) playground; \
	fi

deploy:
	gcloud run deploy $(SERVICE_NAME) \
		--source . \
		--project $(PROJECT_ID) \
		--region $(LOCATION) \
		--no-allow-unauthenticated \
		--set-env-vars GOOGLE_CLOUD_PROJECT=$(PROJECT_ID),GCP_REGION=$(LOCATION),GOOGLE_GENAI_USE_VERTEXAI=TRUE

deploy-agent-engine:
	uv run adk deploy agent_engine \
		--project=$(PROJECT_ID) \
		--region=$(LOCATION) \
		--display_name="$(SERVICE_NAME)" \
		./app

register-gemini-enterprise:
	@APP_ID="$(or $(GEMINI_ENTERPRISE_APP_ID),$(GE_APP_ID))"; \
	if [ -z "$$APP_ID" ]; then \
		echo "Error: Set GEMINI_ENTERPRISE_APP_ID (or GE_APP_ID) to your Gemini Enterprise App ID"; exit 1; \
	fi; \
	uvx agent-starter-pack register-gemini-enterprise \
		--project $(PROJECT_ID) \
		--app-id $$APP_ID \
		--display-name "Retail Virtual Try-On Assistant"

publish-agent-garden:
	@test -f manifest.yaml && grep -q "deployable: true" manifest.yaml || { echo "Error: manifest.yaml must have deployable: true"; exit 1; }
	@test -f agents-cli-manifest.yaml || { echo "Error: agents-cli-manifest.yaml is required for Agent Garden"; exit 1; }
	@test -f Dockerfile || { echo "Error: Dockerfile is required for Agent Garden"; exit 1; }
	@echo "✅ Validated Agent Garden bundle (manifest.yaml deployable: true + agents-cli-manifest.yaml + Dockerfile)"
	@echo "   • Managed Agent Garden Console: https://console.cloud.google.com/vertex-ai/agents/agent-garden?project=$(PROJECT_ID)"
	@echo "   • Deployed Agent Engines Console: https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=$(PROJECT_ID)"
	@echo "   • One-click CLI deploy from Agent Garden catalog: uvx --from google-agents-cli agents-cli create $(SERVICE_NAME)"

test:
	uv run pytest

lint:
	uvx ruff check .
	uvx ruff format --check .

.PHONY: install playground local-backend surface deploy deploy-agent-engine register-gemini-enterprise publish-agent-garden test lint
