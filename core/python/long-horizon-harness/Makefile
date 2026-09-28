.PHONY: dev dev-backend dev-web dev-local dev-backend-local dev-sandbox dev-backend-sandbox \
        dev-preflight dev-ready dev-web-wait dev-stop \
        setup build-web deploy deploy-web deploy-backend deploy-all destroy commands-catalog help

BACKEND_PORT             ?= 8001
WEB_PORT                 ?= 3000
APP_MODULE               ?= app.fast_api_app:app
LHA_ENVIRONMENT_BACKEND ?= local

# PROJECT_ID defaults to whatever `gcloud config get-value project` returns
# so this works for anyone who forks the repo without editing the Makefile.
PROJECT_ID               ?= $(shell gcloud config get-value project 2>/dev/null)
REGION                   ?= us-central1

# Default to a timestamp-based tag so each `make build-web` produces a new
# revision (Cloud Run won't roll forward to a tag it's already on). Override
# with `make build-web WEB_TAG=v3` to pin. The `:=` snapshots the timestamp
# once per make invocation; without it, `$(shell date)` re-fires on every
# expansion and build vs deploy would get different tags.
ifndef WEB_TAG
WEB_TAG := $(shell date +%Y%m%d-%H%M%S)
endif
WEB_REPO                 ?= $(REGION)-docker.pkg.dev/$(PROJECT_ID)/cloud-run-source-deploy/lha-web
LHA_WEB_IMAGE            ?= $(WEB_REPO):$(WEB_TAG)
WEB_SERVICE_NAME         ?= lha-web

# Placeholder image for the first `terraform apply`. agents-cli + gcloud roll
# the real backend/web images next; both Cloud Run services ignore_changes on
# the image, so terraform never reverts them.
PLACEHOLDER_IMAGE        ?= us-docker.pkg.dev/cloudrun/container/hello
# Sandbox BYOC runtime image (only used when LHA_ENVIRONMENT_BACKEND=sandbox);
# the placeholder is fine for a Cloud Run / local-backend deploy.
LHA_RUNTIME_IMAGE        ?= $(PLACEHOLDER_IMAGE)

# Which backend target `make dev` spawns. dev-local / dev-sandbox override this.
DEV_BACKEND_TARGET       ?= dev-backend

help:
	@echo "One-command:"
	@echo "  make dev      From a fresh clone: installs deps, creates .env, runs backend (:$(BACKEND_PORT)) + web (:$(WEB_PORT))."
	@echo "                Backend defaults to local; set LHA_ENVIRONMENT_BACKEND=sandbox in .env or use 'make dev-sandbox' for the Vertex sandbox."
	@echo "  make deploy   Provision infra (Terraform) + deploy backend & web (Cloud Run), one command."
	@echo "  make destroy            terraform destroy — tear down all billable infra."
	@echo ""
	@echo "More targets:"
	@echo "  make setup              Install deps + create .env, without starting servers"
	@echo "  make dev-local          dev forcing LHA_ENVIRONMENT_BACKEND=local (ignores .env)"
	@echo "  make dev-sandbox        dev forcing LHA_ENVIRONMENT_BACKEND=sandbox (ignores .env)"
	@echo "  make dev-backend        Backend only"
	@echo "  make dev-web            Web only"
	@echo "  make dev-stop           Kill whatever is listening on :$(BACKEND_PORT) / :$(WEB_PORT)"
	@echo "  make build-web          Build & push the web image (LHA_WEB_IMAGE=$(LHA_WEB_IMAGE))"
	@echo "  make deploy-web         build-web + roll Cloud Run service $(WEB_SERVICE_NAME) to it"
	@echo "  make deploy-backend     Build & deploy the backend Cloud Run service via agents-cli"

# ---- From-scratch bootstrap (idempotent; fast on repeat via file-target guards) ----

# Create .env from the template only if missing (no prereqs => runs only when absent).
.env:
	@cp .env.example .env && echo ">> created .env from .env.example — set GOOGLE_CLOUD_PROJECT (or leave blank to use your gcloud config project)"

# Install web deps only when missing or package.json changed.
web/node_modules: web/package.json
	cd web && npm install

# Install Python deps; restamp when deps change. Use `uv sync --all-extras` so
# the dev env keeps the lint + eval extras — a default sync (what bare
# `agents-cli install` runs) prunes them, leaving `make` without ruff/ty/codespell.
.uv-installed: pyproject.toml uv.lock
	uv sync --all-extras && touch $@

# Explicit one-shot setup (optional convenience).
setup: .env .uv-installed web/node_modules
	@echo "setup complete — run 'make dev'"

# ---- Dev ----

# A stale stack makes `make dev` half-fail (uvicorn exits on the bound port,
# vite drifts to the next one), so refuse to start rather than look alive.
dev-preflight:
	@command -v lsof >/dev/null 2>&1 || exit 0; \
	busy=""; \
	for p in $(BACKEND_PORT) $(WEB_PORT); do \
	  lsof -ti :$$p -sTCP:LISTEN >/dev/null 2>&1 && busy="$$busy $$p"; \
	done; \
	if [ -n "$$busy" ]; then \
	  echo "ERROR: port(s)$$busy already in use — a dev stack is probably still running."; \
	  echo "       'make dev-stop' kills it, or pass BACKEND_PORT=/WEB_PORT= to use others."; \
	  exit 1; \
	fi

dev-stop:
	@command -v lsof >/dev/null 2>&1 || { echo "lsof not found — stop the dev ports by hand"; exit 1; }; \
	pids=$$(lsof -ti :$(BACKEND_PORT) -i :$(WEB_PORT) -sTCP:LISTEN 2>/dev/null | sort -u); \
	if [ -z "$$pids" ]; then echo "nothing listening on :$(BACKEND_PORT) / :$(WEB_PORT)"; exit 0; fi; \
	echo ">> stopping $$(echo $$pids | tr '\n' ' ')"; \
	kill $$pids 2>/dev/null || true; \
	sleep 1; \
	left=$$(lsof -ti :$(BACKEND_PORT) -i :$(WEB_PORT) -sTCP:LISTEN 2>/dev/null | sort -u); \
	[ -n "$$left" ] && kill -9 $$left 2>/dev/null; \
	true

# Each half kills the process group when it exits, so a dead backend takes the
# web server down with it instead of leaving a stack that only looks alive.
# (`wait -n` would be the one-liner, but /bin/sh here is bash 3.2.)
dev: dev-preflight
	@echo ""
	@echo ">> starting the dev stack — ready in ~30s (a first run installs deps first, so longer)"
	@echo ">> the web UI starts once the backend is listening on :$(BACKEND_PORT)"
	@echo ""
	@trap 'kill 0' INT TERM EXIT; \
	{ $(MAKE) --no-print-directory $(DEV_BACKEND_TARGET) 2>&1 | sed -u 's/^/[backend] /'; echo "[backend] exited — stopping dev stack"; kill 0; } & \
	{ $(MAKE) --no-print-directory dev-web-wait          2>&1 | sed -u 's/^/[web]     /'; echo "[web]     exited — stopping dev stack"; kill 0; } & \
	$(MAKE) --no-print-directory dev-ready & \
	wait

# The first run interleaves npm install + uv sync output for minutes, so vite's
# own URL banner scrolls away long before the stack is usable. Any HTTP reply
# (404 included) means the port is bound, so plain `curl -s` is the readiness
# probe. Dies with the stack via `dev`'s `kill 0` trap.
dev-ready:
	@command -v curl >/dev/null 2>&1 || exit 0; \
	i=0; \
	while [ $$i -lt 600 ]; do \
	  if curl -s -o /dev/null http://127.0.0.1:$(BACKEND_PORT) && curl -s -o /dev/null http://127.0.0.1:$(WEB_PORT); then \
	    echo ""; \
	    echo "=================================================================="; \
	    echo ""; \
	    echo "   READY — open the web UI:"; \
	    echo ""; \
	    echo "       http://localhost:$(WEB_PORT)"; \
	    echo ""; \
	    echo "   backend   http://127.0.0.1:$(BACKEND_PORT)"; \
	    echo "   stop      Ctrl-C"; \
	    echo ""; \
	    echo "=================================================================="; \
	    echo ""; \
	    exit 0; \
	  fi; \
	  i=$$((i+1)); sleep 1; \
	done

# vite proxies /.well-known, /a2a and /lha to the backend from the moment it
# boots, so starting it first buries the terminal in ECONNREFUSED stack traces
# until uvicorn binds. npm install still runs in parallel (the prerequisite);
# only the server start waits.
dev-web-wait: web/node_modules
	@if command -v curl >/dev/null 2>&1; then \
	  i=0; \
	  while [ $$i -lt 600 ] && ! curl -s -o /dev/null http://127.0.0.1:$(BACKEND_PORT); do \
	    i=$$((i+1)); sleep 1; \
	  done; \
	fi; \
	$(MAKE) --no-print-directory dev-web

# .env wins; the -local/-sandbox variants below force a value instead.
DEV_BACKEND_ENV ?= $${LHA_ENVIRONMENT_BACKEND:-$(LHA_ENVIRONMENT_BACKEND)}

dev-backend dev-backend-local dev-backend-sandbox: .env .uv-installed
	set -a; [ -f .env ] && . ./.env; set +a; \
	: $${GOOGLE_CLOUD_PROJECT:=$(PROJECT_ID)}; export GOOGLE_CLOUD_PROJECT; \
	LHA_ENVIRONMENT_BACKEND=$(DEV_BACKEND_ENV) \
	USE_IN_MEMORY_SESSION=1 \
	ALLOW_ORIGINS=http://localhost:$(WEB_PORT),http://127.0.0.1:$(WEB_PORT) \
	uv run uvicorn $(APP_MODULE) --host 127.0.0.1 --port $(BACKEND_PORT) --reload

# --strictPort: a silent drift to :3001 breaks the backend's pinned ALLOW_ORIGINS.
dev-web: web/node_modules
	cd web && NEXT_PUBLIC_LHA_URL=http://127.0.0.1:$(BACKEND_PORT) npx vite --port $(WEB_PORT) --strictPort

dev-local:
	$(MAKE) DEV_BACKEND_TARGET=dev-backend-local dev

dev-sandbox:
	$(MAKE) DEV_BACKEND_TARGET=dev-backend-sandbox dev

# Force a backend, ignoring .env (guaranteed-local / symmetric sandbox override).
dev-backend-local:   DEV_BACKEND_ENV := local
dev-backend-sandbox: DEV_BACKEND_ENV := sandbox

# ---- Deploy ----

# Build the web frontend image via Cloud Build so we don't need a local
# docker daemon, and so the build context picks up `web/` (which now includes
# `web/server/`) from the repo root.
build-web:
	gcloud builds submit . \
		--project $(PROJECT_ID) \
		--config web/server/cloudbuild.yaml \
		--substitutions=_IMAGE=$(LHA_WEB_IMAGE)

deploy-web: build-web
	gcloud run deploy $(WEB_SERVICE_NAME) \
		--project $(PROJECT_ID) \
		--region $(REGION) \
		--image $(LHA_WEB_IMAGE)

# Backend deploy is owned by agents-cli (runs `gcloud beta run deploy lha
# --source .` from the repo root). No image tag flag because the backend
# Cloud Run service uses Cloud Build's auto-tagging.
# agents-cli forwards a fixed env allowlist from the .env FILE (not the shell env),
# whose LOCAL dev values clobber terraform's prod settings — a pre-deploy shell
# `export` is IGNORED. So re-assert the two that break the deploy AFTER the deploy
# via `gcloud run services update` (authoritative, runs last):
#   LHA_AUTH_MODE=iap     — .env's `dev` 500s on Cloud Run (K_SERVICE backstop).
#   LHA_SANDBOX_CALLER_SA — terraform makes the Cloud Run SA double as the sandbox
#     caller (self serviceAccountTokenCreator binding); pull it from the live
#     service so .env's legacy value can't override it (else mint_sandbox_token
#     403s on signJwt). Falls back to .env if the service isn't up yet.
deploy-backend:
	set -a; [ -f .env ] && . ./.env; set +a; \
	: $${GOOGLE_CLOUD_PROJECT:=$(PROJECT_ID)}; export GOOGLE_CLOUD_PROJECT; \
	CALLER_SA=$$(gcloud run services describe lha --project=$$GOOGLE_CLOUD_PROJECT --region=$(REGION) --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || echo "$$LHA_SANDBOX_CALLER_SA"); \
	agents-cli deploy --project=$$GOOGLE_CLOUD_PROJECT --region=$(REGION) --no-confirm-project \
		--min-instances 1 --max-instances 8 --concurrency 8; \
	echo "==> Re-asserting terraform-owned prod env (agents-cli forwarded .env's local dev values)"; \
	gcloud run services update lha --project=$$GOOGLE_CLOUD_PROJECT --region=$(REGION) \
		--update-env-vars "LHA_AUTH_MODE=iap,LHA_SANDBOX_CALLER_SA=$$CALLER_SA"

# One-command deploy (canonical): infra (Terraform) THEN code (agents-cli + web).
# Step 1 provisions Cloud SQL / secrets / IAM / scheduler / IAP / buckets + the
# two Cloud Run skeletons; steps 2-3 roll the real images (services
# ignore_changes on the image, so they don't fight Terraform).
deploy:
	@command -v terraform  >/dev/null 2>&1 || { echo "ERROR: terraform not installed — https://developer.hashicorp.com/terraform/downloads"; exit 1; }
	@command -v agents-cli >/dev/null 2>&1 || { echo "ERROR: agents-cli not installed — https://goo.gle/agents-cli"; exit 1; }
	@test -n "$(PROJECT_ID)" || { echo "ERROR: no GCP project — run 'gcloud config set project <id>' or pass PROJECT_ID=<id>."; exit 1; }
	@echo "==> [1/3] Provisioning infrastructure via Terraform"
	@echo "    (Cloud SQL, Secret Manager, IAM, Cloud Scheduler, IAP, GCS buckets, Cloud Run skeletons)"
	cd terraform && terraform init -input=false && terraform apply -auto-approve \
		-var=project_id=$(PROJECT_ID) \
		-var=region=$(REGION) \
		-var=image_uri=$(PLACEHOLDER_IMAGE) \
		-var=lha_web_image=$(PLACEHOLDER_IMAGE) \
		-var=lha_runtime_image=$(LHA_RUNTIME_IMAGE)
	@echo "==> [2/3] Deploying backend source to Cloud Run (agents-cli)"
	$(MAKE) --no-print-directory deploy-backend
	@echo "==> [3/3] Building + rolling the web image"
	$(MAKE) --no-print-directory deploy-web
	@echo ""
	@echo "==> Deploy complete. If the web UI returns 403, grant yourself IAP access:"
	@echo "    TF_VAR_iap_users='[\"user:you@example.com\"]' make deploy   (re-runs, adds the binding)"

deploy-all: deploy

# Tear down every billable resource Terraform created (Cloud SQL, Cloud Run,
# scheduler, secrets, buckets, IAM). Cloud SQL + Cloud Run default
# deletion_protection=true, so first flip the guards off (an apply), then
# destroy. Same placeholder image vars so Terraform can resolve the config.
destroy:
	cd terraform && terraform apply -auto-approve \
		-var=project_id=$(PROJECT_ID) -var=region=$(REGION) \
		-var=image_uri=$(PLACEHOLDER_IMAGE) -var=lha_web_image=$(PLACEHOLDER_IMAGE) \
		-var=lha_runtime_image=$(LHA_RUNTIME_IMAGE) \
		-var=db_deletion_protection=false -var=cloud_run_deletion_protection=false
	cd terraform && terraform destroy -auto-approve \
		-var=project_id=$(PROJECT_ID) -var=region=$(REGION) \
		-var=image_uri=$(PLACEHOLDER_IMAGE) -var=lha_web_image=$(PLACEHOLDER_IMAGE) \
		-var=lha_runtime_image=$(LHA_RUNTIME_IMAGE) \
		-var=db_deletion_protection=false -var=cloud_run_deletion_protection=false

# ---- Docs ----

# Regenerate docs/commands.md from the slash-command registry (offline).
commands-catalog:
	uv run python scripts/generate_commands_catalog.py
