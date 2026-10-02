SHELL := /bin/bash
SKILL_NAME := retail-product-search
PROJECT_ID ?= $(GOOGLE_CLOUD_PROJECT)
REGION ?= us-central1

# Boolean config flags to toggle skill surfacing personas
SURFACE_GEMINI_SKILL ?= true
SURFACE_AGENTS_SKILL ?= true

# ==============================================================================
# Surface Skill Personas via Boolean Config
# Usage: make surface [SURFACE_GEMINI_SKILL=true|false] [SURFACE_AGENTS_SKILL=true|false]
# ==============================================================================

surface:
	@if [ "$(SURFACE_GEMINI_SKILL)" = "true" ]; then \
		$(MAKE) install-gemini-skill; \
	fi
	@if [ "$(SURFACE_AGENTS_SKILL)" = "true" ]; then \
		$(MAKE) install-agents-skill; \
	fi

install-gemini-skill:
	@rm -rf "$(HOME)/.gemini/skills/$(SKILL_NAME)"
	@mkdir -p "$(HOME)/.gemini/skills/$(SKILL_NAME)" "$(HOME)/.gemini/config/skills"
	@ln -sfn "$(CURDIR)" "$(HOME)/.gemini/config/skills/$(SKILL_NAME)"
	@rsync -a --delete \
		--include='SKILL.md' \
		--include='README.md' \
		--include='pyproject.toml' \
		--include='uv.lock' \
		--include='.env.example' \
		--include='scripts/' --include='scripts/*.py' --include='scripts/*.sh' \
		--include='references/' --include='references/*.md' \
		--include='assets/' --include='assets/*.csv' --include='assets/*.md' --include='assets/*.txt' \
		--exclude='*' \
		"$(CURDIR)/" "$(HOME)/.gemini/skills/$(SKILL_NAME)/"
	@echo "✅ Installed $(SKILL_NAME) into ~/.gemini/skills/$(SKILL_NAME) and ~/.gemini/config/skills/$(SKILL_NAME)"

install-agents-skill:
	@mkdir -p "$(HOME)/.agents/skills"
	@ln -sfn "$(CURDIR)" "$(HOME)/.agents/skills/$(SKILL_NAME)"
	@echo "✅ Installed $(SKILL_NAME) into ~/.agents/skills/$(SKILL_NAME)"

install-skill-local: install-gemini-skill install-agents-skill

setup:
	@test -n "$(PROJECT_ID)" || { echo "Error: Set PROJECT_ID or GOOGLE_CLOUD_PROJECT"; exit 1; }
	@printf -- "---\ngcp_project_id: $(PROJECT_ID)\ngcp_region: $(REGION)\ndata_source: default\nproduct_fields: Extended\n---\n" > design-spec.md
	@trap 'rm -f design-spec.md' EXIT; \
	uv run python scripts/setup.py --config design-spec.md
