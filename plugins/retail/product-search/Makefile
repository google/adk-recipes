SHELL := /bin/bash
SKILL_NAME := retail-product-search
DISPLAY_NAME ?= Retail Product Search
DESCRIPTION ?= Hybrid semantic and keyword retail catalogue search agent
PROJECT_ID ?= capstone-tanvi-01-447109
REGION ?= us-central1
GOOGLE3_DIR ?= /google/src/cloud/$(USER)/MyClient/google3
DEFAULT_GE_APP_ID := projects/991831686961/locations/global/collections/default_collection/engines/gemini-enterprise-17845347_1784534731408
APP_ID := $(or $(GEMINI_ENTERPRISE_APP_ID),$(GE_APP_ID),$(DEFAULT_GE_APP_ID))


# ==============================================================================
# 1. Surface to GE Desktop App (GoGo, Sobi) & Skill Hosts
# ==============================================================================

install-skill-local:
	@rm -rf "$(HOME)/.gemini/skills/$(SKILL_NAME)"
	@mkdir -p "$(HOME)/.agents/skills" "$(HOME)/.gemini/skills/$(SKILL_NAME)" "$(HOME)/.gemini/config/skills"
	@ln -sfn "$(CURDIR)" "$(HOME)/.agents/skills/$(SKILL_NAME)"
	@ln -sfn "$(CURDIR)" "$(HOME)/.gemini/config/skills/$(SKILL_NAME)"
	@rsync -a --delete \
		--include='SKILL.md' \
		--include='README.md' \
		--include='scripts/' --include='scripts/*.py' \
		--include='references/' --include='references/*.md' \
		--include='assets/' --include='assets/*.csv' --include='assets/*.md' --include='assets/*.txt' \
		--exclude='*' \
		"$(CURDIR)/" "$(HOME)/.gemini/skills/$(SKILL_NAME)/"
	@echo "✅ Installed $(SKILL_NAME) into ~/.gemini/skills/$(SKILL_NAME) (Spark Beta ready), ~/.gemini/config/skills, and ~/.agents/skills"




# Usage (on Cloudtop/CitC): make sync-google3-skill [GOOGLE3_DIR=/google/src/cloud/<user>/<ws>/google3]
# Usage (from Mac to Cloudtop via SSH): make sync-google3-skill CLOUDTOP_HOST=<cloudtop-hostname>
sync-google3-skill:
	@DEST="$(GOOGLE3_DIR)/third_party/skills/skills/$(SKILL_NAME)"; \
	if [ -n "$(CLOUDTOP_HOST)" ]; then \
		ssh "$(CLOUDTOP_HOST)" "mkdir -p '$$DEST'" && \
		rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.mypy_cache' --exclude='.env' "$(CURDIR)/" "$(CLOUDTOP_HOST):$$DEST/" && \
		echo "✅ Synced $(SKILL_NAME) to $(CLOUDTOP_HOST):$$DEST"; \
	elif [ -d "$(GOOGLE3_DIR)" ]; then \
		mkdir -p "$$DEST" && \
		rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.mypy_cache' --exclude='.env' "$(CURDIR)/" "$$DEST/" && \
		echo "✅ Synced $(SKILL_NAME) to $$DEST"; \
	else \
		echo "ℹ️  Local Mac detected: '$(GOOGLE3_DIR)' is not mounted on this machine."; \
		echo "   • Local skill hosts (~/.agents/skills & ~/.gemini/skills) are already active via 'make install-skill-local'."; \
		echo "   • To push to a remote Cloudtop CitC client over SSH, run:"; \
		echo "     make -C plugins/retail/product-search sync-google3-skill CLOUDTOP_HOST=<your-cloudtop>.c.googlers.com"; \
	fi

# ==============================================================================
# 2a. Deploy & Surface to GE App (Gemini Enterprise)
# ==============================================================================

setup:
	@printf -- "---\ngcp_project_id: $(PROJECT_ID)\ngcp_region: $(REGION)\ndata_source: default\nproduct_fields: Extended\n---\n" > design-spec.md
	@trap 'rm -f design-spec.md' EXIT; \
	uv run python scripts/setup.py --config design-spec.md

deploy-agent-engine:
	uvx --from google-agents-cli agents-cli deploy \
		--deployment-target agent_runtime \
		$(if $(PROJECT_ID),--project="$(PROJECT_ID)") \
		--region="$(REGION)" \
		--service-name="$(SKILL_NAME)" \
		--no-confirm-project

deploy-cloudrun:
	uvx --from google-agents-cli agents-cli deploy \
		--deployment-target cloud_run \
		$(if $(PROJECT_ID),--project="$(PROJECT_ID)") \
		--region="$(REGION)" \
		--service-name="$(SKILL_NAME)" \
		--no-confirm-project

# Register on Gemini Enterprise via google-agents-cli (ADK native or A2A)
# Usage: make publish-gemini-enterprise [GEMINI_ENTERPRISE_APP_ID="<app-id>"] [AGENT_ENGINE_ID="<engine-id>"]
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


