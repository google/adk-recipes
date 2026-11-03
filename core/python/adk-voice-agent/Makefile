# ADK Voice Agent. Run `make` to list targets.

.DEFAULT_GOAL := help
.PHONY: help install onboard dev docker deploy destroy deploy-tools test lint format \
        computer-image ssh doctor eval eval-sandbox

REGION ?= us-central1
HOST ?= 127.0.0.1

ifneq ($(filter deploy destroy computer-image ssh doctor,$(MAKECMDGOALS)),)
PROJECT := $(or $(PROJECT),$(shell uv run --quiet python scripts/deploy.py --print-project))
endif

TF = terraform -chdir=terraform
TF_VARS = -var=project_id=$(PROJECT) -var=region=$(REGION) $(if $(TFVARS),-var-file=$(abspath $(TFVARS)))
DEPLOY_ARGS = $(if $(PROJECT),--project=$(PROJECT)) --region=$(REGION) $(if $(TFVARS),--var-file=$(abspath $(TFVARS)))
EVAL_ARGS = $(if $(DATASET),--dataset $(DATASET)) $(if $(CASES),--cases $(CASES))

help:
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-z-]+:.*## / {printf "  make %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Get started

onboard: install ## Set up a deployment: the GCP project, the Google and GitHub apps, then terraform, computer image, and deploy
	uv run python -m scripts.onboard $(DEPLOY_ARGS) $(if $(DRY_RUN),--dry-run)

dev: install ## API on :8000 + UI on :3000 with hot reload (HOST=0.0.0.0 for your LAN); Ctrl-C stops both
	@HOST=$(HOST) sh scripts/dev.sh

##@ Ship (Cloud Run behind IAP)

docker: ## Build the production image and run it on :8080 (the container `make deploy` ships)
	@test -f .env || { echo "ERROR: no .env yet. Run 'make onboard' first."; exit 1; }
	@test -f "$(HOME)/.config/gcloud/application_default_credentials.json" || { echo "ERROR: no credentials at $(HOME)/.config/gcloud/application_default_credentials.json. Run 'gcloud auth application-default login'."; exit 1; }
	docker build -t adk-voice-agent:local .
	docker run --rm -it -p $(HOST):8080:8080 -v "$(CURDIR)/.env:/code/.env:ro" \
		-v "$(HOME)/.config/gcloud/application_default_credentials.json:/home/voice_agent/.config/gcloud/application_default_credentials.json:ro" adk-voice-agent:local

# The computer image is resolved first, so an unpinned deploy keeps the running one, never `:latest`.
deploy: deploy-tools ## Terraform, then deploy the app (DRY_RUN=1 previews both, AUTO_APPROVE=1 skips approval)
	$(TF) init -input=false
	image=$$(uv run --quiet python scripts/deploy.py --print-computer-image $(DEPLOY_ARGS) $(if $(COMPUTER_IMAGE),--computer-image=$(COMPUTER_IMAGE))) && \
	$(TF) $(if $(DRY_RUN),plan,apply $(if $(AUTO_APPROVE),-auto-approve -input=false)) $(TF_VARS) -var=computer_image=$$image
	uv run python scripts/deploy.py $(DEPLOY_ARGS) $(if $(DRY_RUN),--dry-run)

# Computers live outside terraform's state, so they go first; then Cloud SQL's deletion protection is lifted.
destroy: deploy-tools ## Delete this deployment's computers (home disks only with PURGE=1), then everything terraform created; doesn't ask
	$(TF) init -input=false
	uv run python scripts/deploy.py $(DEPLOY_ARGS) --delete-computers $(if $(PURGE),--purge)
	$(TF) apply -auto-approve $(TF_VARS) $(if $(COMPUTER_IMAGE),-var=computer_image=$(COMPUTER_IMAGE)) -var=db_deletion_protection=false -target=google_sql_database_instance.db
	$(TF) destroy -auto-approve $(TF_VARS) $(if $(COMPUTER_IMAGE),-var=computer_image=$(COMPUTER_IMAGE)) -var=db_deletion_protection=false
	@echo "Left in place: the users' home disks without a snapshot schedule (unless PURGE=1), existing disk snapshots (expire in 7 days), and per-user connected-app token secrets (<service>-user-*)."

##@ Develop

BIOME_VERSION := 2.5.10
BIOME := .cache/bin/biome
BIOME_OS := $(shell uname -s | tr A-Z a-z)
BIOME_ARCH := $(patsubst x86_64,x64,$(patsubst aarch64,arm64,$(shell uname -m)))
BIOME_FMT_ARGS := --indent-style=space --indent-width=2 --line-ending=lf --line-width=80 \
	--javascript-formatter-quote-style=double --jsx-quote-style=double --semicolons=always \
	--trailing-commas=all --bracket-spacing=true --arrow-parentheses=always \
	--json-formatter-trailing-commas=none
RUFF_CHECK_ARGS = $(if $(wildcard ../../../pyproject.toml),,--line-length=80 --target-version=py311 \
	--select=E,F,I,C,PL,B,UP,RUF,S \
	--ignore=E501,PLR0913,PLR2004,PLC0415,PLR0917,PLR0911,PLR0912,PLR0915,C901,PLW0603 \
	--config 'src = ["."]' \
	--config 'lint.per-file-ignores = {"**/__init__.py" = ["F401"], "**/tests/**" = ["S101","S104","S105","S106","S107","S108","S110","S113","S310","S311","S324","S603","S607"], "**/test_*.py" = ["S101","S104","S105","S106","S107","S108","S110","S113","S310","S311","S324","S603","S607"]}')
RUFF_FORMAT_ARGS = $(if $(wildcard ../../../pyproject.toml),,--line-length=80)

test: ## Unit, runnability, and web tests (offline)
	uv run pytest tests/unit tests/test_runnability.py
	npm --prefix web test

lint: $(BIOME) ## ruff, codespell, ty, mypy, Biome, and the web typecheck
	uv run --extra lint ruff check $(RUFF_CHECK_ARGS) . && uv run --extra lint ruff format $(RUFF_FORMAT_ARGS) --check .
	uv run --extra lint codespell --toml pyproject.toml
	uv run --extra lint ty check app
	uvx mypy --ignore-missing-imports --follow-imports=silent --exclude 'tests/eval/fixtures' app scripts tests workspace_mcp sandbox
	$(BIOME) format $(BIOME_FMT_ARGS) web/src && $(BIOME) lint --error-on-warnings web/src
	npm --prefix web run typecheck

format: $(BIOME) ## Apply ruff and Biome formatting with the adk-recipes rules
	uv run --extra lint ruff check --fix $(RUFF_CHECK_ARGS) . && uv run --extra lint ruff format $(RUFF_FORMAT_ARGS) .
	$(BIOME) format --write $(BIOME_FMT_ARGS) web/src

##@ Your computer

# Owner is hashed from your gcloud account to match IAP identity.
COMPUTER_SSH = uv run --quiet python -m scripts.computer_ssh $(DEPLOY_ARGS)

ssh: deploy-tools ## SSH into your computer through IAP and open a shell in its container
	@$(COMPUTER_SSH) -- -t sudo docker exec -it -u agent computer bash -l

doctor: deploy-tools ## Check your computer: harness versions, Vertex AI auth, MCP reachability, disk space
	@$(COMPUTER_SSH) -- sudo docker exec -i -u agent computer sh -s < scripts/doctor.sh

computer-image: deploy-tools ## Build sandbox/Dockerfile with Cloud Build, push it, and print how to roll it out
	uv run python -m scripts.provisioning.sandbox_image --project=$(PROJECT) --region=$(REGION)

##@ Evaluate

eval: ## System 1 voice evals over ADK Live (DATASET=..., CASES=...), graded into artifacts/system1/
	uv run python scripts/run_eval.py --suite system1 $(EVAL_ARGS)

eval-sandbox: ## System 2 computer evals on docker computers (HARNESS=claude, CASES=..., NO_BUILD=1)
	uv run python scripts/run_eval.py --suite system2 $(if $(HARNESS),--harness $(HARNESS)) $(EVAL_ARGS) $(if $(NO_BUILD),--no-build)

install: .venv web/node_modules

.venv: pyproject.toml uv.lock
	uv sync --all-extras
	@touch $@

web/node_modules: web/package.json web/package-lock.json
	npm --prefix web ci
	@touch $@

$(BIOME):
	@mkdir -p $(dir $@)
	curl -sSfL --max-time 120 -o $@ "https://github.com/biomejs/biome/releases/download/%40biomejs%2Fbiome%40$(BIOME_VERSION)/biome-$(BIOME_OS)-$(BIOME_ARCH)"
	@chmod +x $@

deploy-tools:
	@command -v terraform >/dev/null || { echo "ERROR: terraform is not installed: https://developer.hashicorp.com/terraform/install"; exit 1; }
	@command -v gcloud >/dev/null || { echo "ERROR: gcloud is not installed: https://cloud.google.com/sdk/docs/install"; exit 1; }
	@test -n "$(PROJECT)" || { echo "ERROR: no GCP project. Set GOOGLE_CLOUD_PROJECT in .env, run 'gcloud config set project <id>', or pass PROJECT=<id>."; exit 1; }
