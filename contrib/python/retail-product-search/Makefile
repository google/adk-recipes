SHELL := /bin/bash
-include .env
export

SERVICE_NAME := retail-product-search
DISPLAY_NAME ?= Retail Product Search
DESCRIPTION ?= Hybrid semantic and keyword retail catalogue search agent
PROJECT_ID ?= $(GOOGLE_CLOUD_PROJECT)
REGION ?= us-central1
APP_ID := $(or $(GEMINI_ENTERPRISE_APP_ID),$(GE_APP_ID))
PORT ?= 8080

# Boolean config flags to toggle agent surfacing personas
DEPLOY_AGENT_ENGINE ?= true
PUBLISH_GEMINI_ENTERPRISE ?= true
PUBLISH_AGENT_GARDEN ?= true
DEPLOY_CLOUD_RUN ?= false
RUN_LOCAL_WEB ?= false

# ==============================================================================
# Surface Agent Personas via Boolean Config
# Usage: make surface [DEPLOY_AGENT_ENGINE=true|false] [PUBLISH_GEMINI_ENTERPRISE=true|false] [PUBLISH_AGENT_GARDEN=true|false] [DEPLOY_CLOUD_RUN=true|false] [RUN_LOCAL_WEB=true|false]
# ==============================================================================

surface:
	@if [ "$(DEPLOY_AGENT_ENGINE)" = "true" ]; then \
		$(MAKE) deploy-agent-engine; \
	fi
	@if [ "$(PUBLISH_GEMINI_ENTERPRISE)" = "true" ]; then \
		$(MAKE) publish-gemini-enterprise; \
	fi
	@if [ "$(PUBLISH_AGENT_GARDEN)" = "true" ]; then \
		$(MAKE) publish-agent-garden; \
	fi
	@if [ "$(DEPLOY_CLOUD_RUN)" = "true" ]; then \
		$(MAKE) deploy-cloudrun; \
	fi
	@if [ "$(RUN_LOCAL_WEB)" = "true" ]; then \
		$(MAKE) run-local; \
	fi

deploy-agent-engine:
	uvx --from google-agents-cli agents-cli deploy \
		--deployment-target agent_runtime \
		$(if $(PROJECT_ID),--project="$(PROJECT_ID)") \
		--region="$(REGION)" \
		--service-name="$(SERVICE_NAME)" \
		--no-confirm-project

publish-gemini-enterprise:
	uvx --from google-agents-cli agents-cli publish gemini-enterprise \
		--interactive \
		$(if $(PROJECT_ID),--project-id="$(PROJECT_ID)") \
		$(if $(APP_ID),--gemini-enterprise-app-id="$(APP_ID)") \
		$(if $(REGISTRATION_TYPE),--registration-type="$(REGISTRATION_TYPE)") \
		$(if $(AGENT_ENGINE_ID),--agent-runtime-id="$(AGENT_ENGINE_ID)") \
		$(if $(AGENT_CARD_URL),--agent-card-url="$(AGENT_CARD_URL)") \
		--display-name="$(DISPLAY_NAME)" \
		--description="$(DESCRIPTION)" \
		--tool-description="$(DESCRIPTION)"

register-gemini-enterprise: publish-gemini-enterprise

publish-agent-garden:
	@test -f manifest.yaml && grep -q "deployable: true" manifest.yaml || { echo "Error: manifest.yaml must have deployable: true"; exit 1; }
	@test -f agents-cli-manifest.yaml || { echo "Error: agents-cli-manifest.yaml is required for Agent Garden"; exit 1; }
	@test -f Dockerfile || { echo "Error: Dockerfile is required for Agent Garden"; exit 1; }
	@echo "✅ Validated Agent Garden bundle (manifest.yaml deployable: true + agents-cli-manifest.yaml + Dockerfile)"
	@echo "   • Managed Agent Garden Console: https://console.cloud.google.com/vertex-ai/agents/agent-garden?project=$(PROJECT_ID)"
	@echo "   • Deployed Agent Engines Console: https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=$(PROJECT_ID)"
	@echo "   • One-click CLI deploy from Agent Garden catalog: uvx --from google-agents-cli agents-cli create $(SERVICE_NAME)"

deploy-cloudrun:
	uvx --from google-agents-cli agents-cli deploy \
		--deployment-target cloud_run \
		$(if $(PROJECT_ID),--project="$(PROJECT_ID)") \
		--region="$(REGION)" \
		--service-name="$(SERVICE_NAME)" \
		--no-confirm-project

run-local:
	uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port $(PORT)
