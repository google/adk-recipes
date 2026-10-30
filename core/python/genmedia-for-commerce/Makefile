# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

SHELL := /bin/bash

# Shell-safe KEY='value' lines parsed from config.env by python-dotenv; recipes eval this instead of `source config.env`
CONFIG_SH = uv run python -c "import shlex; from dotenv import dotenv_values; print('\n'.join(f'{k}={shlex.quote(v)}' for k, v in dotenv_values('config.env').items() if v))"

# Cloud Run env-vars YAML: the non-empty config.env values plus COMMIT_SHA. Keys ending in _KEY, _SECRET, _TOKEN, _PASSWORD or _CREDENTIALS are left out with a warning (plain env vars are readable by anyone who can view the service: keep them in Secret Manager and pass them with SECRETS=)
CLOUDRUN_ENV_YAML = uv run python -c "import json, os, sys; from dotenv import dotenv_values; env = {k: v for k, v in dotenv_values('config.env').items() if v}; secret = [k for k in env if k.endswith(('_KEY', '_SECRET', '_TOKEN', '_PASSWORD', '_CREDENTIALS'))]; secret and print('WARNING: not deployed, as they look like secrets:', ', '.join(secret) + '. Store them in Secret Manager and pass them with make deploy SECRETS=NAME=SECRET_ID:latest,...', file=sys.stderr); env = {k: v for k, v in env.items() if k not in secret} | {'COMMIT_SHA': os.environ['COMMIT_SHA']}; print('\n'.join(f'{k}: {json.dumps(v)}' for k, v in env.items()))"

# infra/web_proxy.sh with the service of config.env (after eval "$$($(CONFIG_SH))"): gcloud run services proxy on :8080 [--hint | --background]
WEB_PROXY = PROJECT_ID="$$PROJECT_ID" DEFAULT_REGION="$$DEFAULT_REGION" SERVICE_NAME="$$SERVICE_NAME" bash infra/web_proxy.sh

# The account make web mints the proxy's identity token as (make setup-infra creates it), and the service's location flags (after eval "$$($(CONFIG_SH))")
WEB_SA = genmedia-web@$$PROJECT_ID.iam.gserviceaccount.com
SERVICE_FLAGS = $${SERVICE_NAME:-genmedia-for-commerce} --region=$${DEFAULT_REGION:-us-central1} --project=$$PROJECT_ID

# After a deploy: let genmedia-web invoke the service (idempotent). Never fails the deploy; a setup older than genmedia-web gets told to run make setup-infra
GRANT_WEB_INVOKER = out=$$(gcloud run services add-iam-policy-binding $(SERVICE_FLAGS) --member=serviceAccount:$(WEB_SA) --role=roles/run.invoker --quiet 2>&1 >/dev/null) && echo "$(WEB_SA) can invoke the service (make web acts as it)." || { echo "$$out" | grep -qi "does not exist" && echo "No $(WEB_SA) yet (set up by an older version): run make setup-infra, then make deploy, so make web works in Cloud Shell too." || echo "Warning: could not let $(WEB_SA) invoke the service (make web falls back to your own identity): $$out"; }

# Terraform on PATH, or a pinned release that infra/terraform.sh installs into ~/.local/bin
TERRAFORM = bash infra/terraform.sh

# Ruff with the rules of the adk-recipes root pyproject.toml, which lints the
# copy of this recipe in google/adk-recipes (a recipe may not carry its own
# ruff config). --isolated: the same result inside or outside that repository
RUFF_TEST_IGNORES = ["S101","S104","S105","S106","S107","S108","S110","S113","S310","S311","S324","S603","S607"]
RUFF_CONFIG = --isolated \
	--config 'line-length = 80' \
	--config 'target-version = "py311"' \
	--config 'lint.isort.known-first-party = ["genmedia4commerce"]' \
	--config 'lint.per-file-ignores = {"**/__init__.py" = ["F401"], "**/tests/**" = $(RUFF_TEST_IGNORES), "**/test_*.py" = $(RUFF_TEST_IGNORES)}'
RUFF_RULES = --select E,F,I,C,PL,B,UP,RUF,S \
	--ignore E501,PLR0913,PLR2004,PLC0415,PLR0917,PLR0911,PLR0912,PLR0915,C901,PLW0603

# mypy with the flags of adk-recipes' CI, which type-checks the copy of this
# recipe from its own root (so no mypy config here would be read)
MYPY = uvx mypy --ignore-missing-imports --follow-imports=silent --explicit-package-bases

# ==============================================================================
# Installation & Setup
# ==============================================================================

# Install Python dependencies (incl. dev group) with uv
install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.8.13/install.sh | sh; source $$HOME/.local/bin/env; }
	uv sync

# ==============================================================================
# Playground Targets
# ==============================================================================

# Optional, best effort: the ADK web UI on :8501, the agent without the Studio panel (the supported local run is make dev)
# Launch the ADK web playground on :8501 (sessions in the media bucket:
# services.yaml registers studio://)
playground:
	@echo "==============================================================================="
	@echo "| 🚀 Starting your agent playground...                                        |"
	@echo "|                                                                             |"
	@echo "| 🔍 IMPORTANT: Select the 'genmedia4commerce' folder to interact with your agent.          |"
	@echo "==============================================================================="
	uv run adk web . --port 8501 --reload_agents --session_service_uri=studio://

# ==============================================================================
# Deployment
# ==============================================================================

# Create config.env from config.env.example when it is missing, with PROJECT_ID set to the gcloud project
config.env:
	@project=$$(gcloud config get-value project 2>/dev/null); \
	if [ -z "$$project" ]; then \
		echo "Error: no config.env and no gcloud project. Copy config.env.example to config.env and set PROJECT_ID."; exit 1; \
	fi; \
	sed "s/^PROJECT_ID=.*/PROJECT_ID=$$project/" config.env.example > config.env && \
	echo "Created config.env from config.env.example with PROJECT_ID=$$project (the gcloud project). Edit config.env to change it."

# Deploy the app (web app, REST API, ADK API, Studio MCP App at /mcp) as one Cloud Run service, from source, built as the genmedia-build account (make setup-infra creates both accounts); then lets genmedia-web invoke the service (make web) and starts the web app proxy on :8080 in the background if the terminal is interactive and the port free, otherwise prints how to open the web app; sizing, env vars and USE_IAP from config.env [USE_IAP=true] [SECRETS="KEY=SECRET_ID:latest,..."]
deploy: config.env
	@eval "$$($(CONFIG_SH))" && \
	env_file=$$(mktemp) && trap 'rm -f "$$env_file"' EXIT && \
	COMMIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo manual) $(CLOUDRUN_ENV_YAML) > "$$env_file" && \
	gcloud config set project $$PROJECT_ID --quiet && \
	log=$$(mktemp) && trap 'rm -f "$$env_file" "$$log"' EXIT && \
	for attempt in 1 2 3; do \
	gcloud run deploy $${SERVICE_NAME:-genmedia-for-commerce} \
		--quiet \
		--source=. \
		--region=$${DEFAULT_REGION:-us-central1} \
		--memory=$$MEMORY \
		--cpu=$$CPU \
		--timeout=$$TIMEOUT \
		--concurrency=$$CONCURRENCY \
		--min-instances=$$MIN_INSTANCES \
		--max-instances=$$MAX_INSTANCES \
		--service-account=genmedia-cloudrun@$$PROJECT_ID.iam.gserviceaccount.com \
		--build-service-account=projects/$$PROJECT_ID/serviceAccounts/genmedia-build@$$PROJECT_ID.iam.gserviceaccount.com \
		--no-allow-unauthenticated \
		--no-cpu-throttling \
		--labels=created-by=adk \
		--env-vars-file="$$env_file" \
		$(if $(filter command line,$(origin SECRETS)),--set-secrets="$(SECRETS)") \
		$$([ "$(if $(filter command line,$(origin USE_IAP)),$(USE_IAP),$$USE_IAP)" = true ] && echo --iap) \
		2>&1 | tee "$$log"; \
	[ "$${PIPESTATUS[0]}" = 0 ] && { echo; $(GRANT_WEB_INVOKER); $(WEB_PROXY) --background || true; exit 0; }; \
	grep -q "missing required IAM permissions" "$$log" && [ $$attempt -lt 3 ] || exit 1; \
	echo "The build account's new roles are still propagating (right after make setup-infra); retrying in 90 s..."; \
	sleep 90; \
	done

# Open the deployed web app at http://localhost:8080 through gcloud run services proxy with an identity token of genmedia-web (else your own), refreshed every 50 minutes, in the foreground; in Cloud Shell use Web Preview on port 8080
web: config.env
	@eval "$$($(CONFIG_SH))" && $(WEB_PROXY)

# Let teammates open the web app with make web: Token Creator on genmedia-web (to mint its identity token) and Cloud Run Viewer on the service only (make web reads its URL) USERS="user:a@example.com,group:team@example.com"
share: config.env
	@[ -n "$(USERS)" ] || { echo 'Usage: make share USERS="user:a@example.com,group:team@example.com"'; exit 1; }
	@eval "$$($(CONFIG_SH))" && \
	for member in $$(echo "$(USERS)" | tr ',' ' '); do \
		gcloud iam service-accounts add-iam-policy-binding $(WEB_SA) --project=$$PROJECT_ID --member="$$member" --role=roles/iam.serviceAccountTokenCreator --quiet >/dev/null && \
		gcloud run services add-iam-policy-binding $(SERVICE_FLAGS) --member="$$member" --role=roles/run.viewer --quiet >/dev/null && \
		echo "$$member: roles/iam.serviceAccountTokenCreator on $(WEB_SA) and roles/run.viewer on the service $${SERVICE_NAME:-genmedia-for-commerce}; they can run make web." || exit 1; \
	done

# ==============================================================================
# Testing & Code Quality
# ==============================================================================

# Legacy: Agent Engine is no longer used; the whole app runs on Cloud Run (make deploy).
# Agent Garden's current command still ends with `make deploy-agent-engine`, so this alias
# deploys the same Cloud Run service. Remove it once Agent Garden runs `make deploy`.
deploy-agent-engine: deploy

# Run the hermetic test suite (no live Google API calls). Projects created with agent-starter-pack also get its sample tests under tests/integration, tests/unit and tests/load_test; they are not part of this app
test:
	uv run pytest tests -q -m "not live" --ignore=tests/integration --ignore=tests/unit --ignore=tests/load_test

# Run live tests against real Google APIs (needs config.env and ADC)
test-live:
	GENMEDIA_LIVE=1 uv run pytest tests/live -q

# Run code quality checks (codespell, ruff lint, ruff format) with the adk-recipes rules; fails when uv.lock is out of date (run uv lock)
lint:
	uv sync --dev --locked
	uv run codespell
	uv run ruff check $(RUFF_CONFIG) $(RUFF_RULES) .
	uv run ruff format $(RUFF_CONFIG) --check --diff .

# Type-check the package, the tests and infra/ with mypy, as adk-recipes' CI does
typecheck:
	$(MYPY) $$(find genmedia4commerce -name '*.py')
	$(MYPY) $$(find tests -name '*.py')
	$(MYPY) $$(find infra -name '*.py')

# Look for secrets in the tracked files (detect-secrets, as the pre-commit hook; known false positives are in .secrets.baseline, internal-only: skipped without it)
secrets:
	@# On a copy of the baseline: the hook rewrites line numbers that moved
	@# and then fails; only a new finding fails this target.
	@[ -f .secrets.baseline ] || { echo "detect-secrets: skipped (no baseline)"; exit 0; }; \
	dir=$$(mktemp -d) && trap 'rm -rf "$$dir"' EXIT && cp .secrets.baseline "$$dir/baseline" && \
	git ls-files -z | grep -zv -E '(^|/)package-lock\.json$$|^\.secrets\.baseline$$' \
		| xargs -0 uv run detect-secrets-hook --baseline "$$dir/baseline" > "$$dir/out" 2>&1; \
	status=$$?; cat "$$dir/out"; \
	if grep -q "Potential secrets" "$$dir/out"; then exit 1; fi; \
	if [ $$status -ne 0 ] && ! grep -q "baseline file was updated" "$$dir/out"; then exit $$status; fi; \
	[ $$status -eq 0 ] || diff .secrets.baseline "$$dir/baseline" || true; \
	echo "detect-secrets: no new secrets"

# Check the syntax of the Gemini Enterprise panel's script (studio.html has no build step)
check-panel:
	@dir=$$(mktemp -d) && trap 'rm -rf "$$dir"' EXIT && \
	python3 -c "import re, sys; s = open('genmedia4commerce/mcp/static/studio.html').read(); scripts = re.findall(r'<script>(.*?)</script>', s, re.S); assert scripts, 'no <script> in studio.html'; sys.stdout.write(chr(10).join(scripts))" > "$$dir/studio.js" && \
	node --check "$$dir/studio.js" && echo "studio.html script: OK"

# Check the Terraform files' format and configuration (no Google Cloud access needed)
tf-check:
	$(TERRAFORM) -chdir=infra/terraform fmt -check -recursive
	$(TERRAFORM) -chdir=infra/terraform init -backend=false -input=false
	$(TERRAFORM) -chdir=infra/terraform validate

# Fix what ruff can fix and format the code, with the same rules as lint [FILES="a.py b.py"; default: everything]
format:
	uv run ruff check $(RUFF_CONFIG) $(RUFF_RULES) --fix $(or $(FILES),.)
	uv run ruff format $(RUFF_CONFIG) $(or $(FILES),.)

# ==============================================================================
# Gemini Enterprise Integration
# ==============================================================================

# Register the Studio MCP App (<Cloud Run URL>/mcp) in its own Gemini Enterprise app, guiding the console-only steps [GEMINI_ENTERPRISE_APP_ID=existing app: projects/.../engines/<id> or <id>] [GEMINI_ENTERPRISE_LOCATION=global] [DRY_RUN=1]
# Then (not in a dry run), whether it succeeded or not, it starts the web app proxy on :8080 in the background if the terminal is interactive and the port free, otherwise prints how to open the web app; it exits with the registration's status
register-gemini-enterprise: config.env
	@uv run python infra/gemini_enterprise.py $(if $(DRY_RUN),--dry-run); status=$$?; \
	if [ -z "$(DRY_RUN)" ]; then echo; eval "$$($(CONFIG_SH))" && $(WEB_PROXY) --background || true; fi; \
	exit $$status

# Allow custom MCP connectors (which make register-gemini-enterprise needs) in the Gemini Enterprise app's project only: the per-project override of the organisation policy discoveryengine.managed.disableCustomMcpServerConnector (enforce: false). Needs the Organization Policy Administrator role
allow-mcp-connectors: config.env
	@uv run python infra/gemini_enterprise.py --allow-mcp-connectors

# ==============================================================================
# Local Development
# ==============================================================================

# Run backend + frontend together; the first to exit stops both (poll loop, works with macOS bash 3.2)
dev:
	@$(MAKE) run-backend & PID1=$$!; \
	$(MAKE) run-frontend & PID2=$$!; \
	trap "kill $$PID1 $$PID2 2>/dev/null" EXIT INT TERM; \
	while true; do \
		if ! kill -0 $$PID1 2>/dev/null; then wait $$PID1; exit $$?; fi; \
		if ! kill -0 $$PID2 2>/dev/null; then wait $$PID2; exit $$?; fi; \
		sleep 1; \
	done

# Run the FastAPI server on 127.0.0.1:8000 (ADK agent API + REST endpoints + Studio MCP App at /mcp + built frontend).
# ALLOW_ORIGINS admits the Vite dev server on :3000: its proxy rewrites Host to :8000 but
# forwards the browser's Origin, which ADK's origin check would otherwise refuse on POSTs.
# This environment value wins over config.env's (the package loads it without override)
run-backend:
	ALLOW_ORIGINS=$${ALLOW_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000} uv run uvicorn genmedia4commerce.fast_api_app:app --host 127.0.0.1 --port 8000 --reload

# Build the frontend and start the Vite dev server on :3000
run-frontend:
	@cd frontend && npm install && npm run build && npm run dev

# ==============================================================================
# Infrastructure (Terraform)
# ==============================================================================

# Helper to export config.env as TF_VAR_ environment variables
define TF_VARS
eval "$$($(CONFIG_SH))" && \
export TF_VAR_project_id=$${PROJECT_ID} && \
export TF_VAR_region=$${DEFAULT_REGION:-us-central1} && \
export TF_VAR_deployer_email=$$(gcloud config get-value account 2>/dev/null) && \
export TF_VAR_service_name=$${SERVICE_NAME:-genmedia-for-commerce}
endef

# Setup with Terraform: APIs, Artifact Registry, media bucket, the runtime, build and web proxy service accounts, IAM. Run it once per project, and again after an upgrade, before make deploy (it creates genmedia-build, which make deploy builds as)
setup-infra: config.env
	@echo "Setting up infrastructure..."
	@eval "$$($(CONFIG_SH))" && \
		gcloud config set project $$PROJECT_ID --quiet && \
		gcloud services enable cloudresourcemanager.googleapis.com --quiet
	@# A fresh project's new APIs and service agents take a minute to be
	@# usable (403 or "does not exist"): terraform apply is safe to re-run.
	@$(TF_VARS) && $(TERRAFORM) -chdir=infra/terraform init && \
	for attempt in 1 2 3; do \
		$(TERRAFORM) -chdir=infra/terraform apply && exit 0; \
		[ $$attempt -lt 3 ] && echo "Terraform failed (attempt $$attempt of 3): new APIs and permissions can take a minute to propagate. Trying again in 30 s (press Ctrl+C to stop)..." && sleep 30; \
	done; exit 1

# Preview Terraform changes
tf-plan: config.env
	@echo "Planning Terraform changes..."
	@$(TF_VARS) && $(TERRAFORM) -chdir=infra/terraform init -input=false && $(TERRAFORM) -chdir=infra/terraform plan

# Destroy Terraform-managed infrastructure
tf-destroy: config.env
	@echo "Destroying Terraform resources..."
	@$(TF_VARS) && $(TERRAFORM) -chdir=infra/terraform init -input=false && $(TERRAFORM) -chdir=infra/terraform destroy
