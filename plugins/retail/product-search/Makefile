SHELL := /bin/bash
SKILL_NAME := retail-product-search
PROJECT_ID ?= $(or $(GOOGLE_CLOUD_PROJECT),your-gcp-project-id)
REGION ?= us-central1
GOOGLE3_DIR ?= /google/src/cloud/$(USER)/MyClient/google3

# Boolean config flags to toggle skill surfacing personas
SURFACE_LOCAL_SKILL ?= true
SURFACE_GOOGLE3_SKILL ?= false

# ==============================================================================
# Surface Skill Personas via Boolean Config
# Usage: make surface [SURFACE_LOCAL_SKILL=true|false] [SURFACE_GOOGLE3_SKILL=true|false]
# ==============================================================================

surface:
	@if [ "$(SURFACE_LOCAL_SKILL)" = "true" ]; then \
		$(MAKE) install-skill-local; \
	fi
	@if [ "$(SURFACE_GOOGLE3_SKILL)" = "true" ]; then \
		$(MAKE) sync-google3-skill; \
	fi

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
	@echo "✅ Installed $(SKILL_NAME) into ~/.gemini/skills/$(SKILL_NAME) (Spark Beta / GoGo / Sobi ready), ~/.gemini/config/skills, and ~/.agents/skills"

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
		echo "   • Local skill hosts (~/.agents/skills & ~/.gemini/skills) are active via 'make install-skill-local'."; \
		echo "   • To push to a remote Cloudtop CitC client over SSH, run:"; \
		echo "     make -C plugins/retail/product-search sync-google3-skill CLOUDTOP_HOST=<your-cloudtop>.c.googlers.com"; \
	fi

setup:
	@printf -- "---\ngcp_project_id: $(PROJECT_ID)\ngcp_region: $(REGION)\ndata_source: default\nproduct_fields: Extended\nsurface_local_skill: $(SURFACE_LOCAL_SKILL)\nsurface_google3_skill: $(SURFACE_GOOGLE3_SKILL)\n---\n" > design-spec.md
	@trap 'rm -f design-spec.md' EXIT; \
	uv run python scripts/setup.py --config design-spec.md
