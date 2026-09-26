# CI/CD with Google Cloud Build

The project uses **Google Cloud Build** for continuous integration and deployment. All triggers target the `main` branch. Environment promotions happen via **git tags**, not branch promotions.

## Strategy Overview

```
Push to main         ──► dev deploy (CI + CD on every push)
Tag v*.*.*-rc.*      ──► staging: full deploy + load tests + post-deploy eval
Tag v*.*.*           ──► prod: shadow deploy + post-deploy eval gate + canary enable
Nightly (00:00 UTC)  ──► regression monitoring + canary promote or rollback
```

**Key design principles:**

- Single `main` branch: no develop/staging branches to manage.
- PRs auto-detect changes to `customer_support_mas/` and run integration tests automatically when agent files are touched. No manual trigger needed.
- Git tags control all environment deployments beyond dev.
- Every production release creates a **new Agent Engine** resource and a **zero-traffic Cloud Run shadow revision**. Eval runs against the shadow. Canary traffic is enabled only after eval passes.
- Nightly eval monitors prod for regressions and auto-promotes or rolls back the canary.
- Terraform is fully decoupled from app deployment.

---

## Pipeline Configs

```
cloudbuild/pr-checks.yaml          Fast CI on every PR to main
cloudbuild/cloudbuild-deploy.yaml  CI + dev deploy on every push to main
cloudbuild/release-staging.yaml    Staging release on tag v*.*.*-rc.*
cloudbuild/release.yaml            Prod canary release on tag v*.*.*
cloudbuild/cloudbuild-nightly.yaml Nightly regression monitor + canary decision
cloudbuild/terraform-plan.yaml     Infra plan on every PR to main
cloudbuild/terraform-apply.yaml    Infra apply on every push to main
```

---

## Job Dependency Graphs

### pr-checks.yaml (every PR to main)

```
detect-agent-changes ─────────────────────────────┐
install-deps                                       │
  ├── lint                                         │
  └── tool-tests                                   │
      └── unit-tests ◄──────────────────────────── ┘
          └── integration-tests  (skipped if no customer_support_mas/ changes)
```

Profile is auto-set: `fast` if no agent files changed, `standard` if they did.

### cloudbuild-deploy.yaml (push to main, dev)

```
detect-agent-changes (3 flags: run_tests, deploy_agent_engine, run_deploy)
get-project-number ──────────────────────────────────┐
install-deps                                         │
  ├── lint ─────────────────────────────┐            │
  └── tool-tests  (skipped if no code)  │            │
      └── unit-tests                    │            │
          └── integration-tests         │            │
              └── docker-build ◄────────┘            │
                  (skipped if no app code)            │
                  └── docker-push                    │
                      └── deploy-agent-engine ◄──────┘
                          (skipped if no agent changes)
                          └── deploy-cloud-run (100% traffic)
                              (skipped if no app code)
                              └── get-service-url
                                  └── smoke-test
                                      └── load-test
```

### release-staging.yaml (tag v*.*.*-rc.*, staging)

```
get-project-number ────────────────────────────────┐
install-deps                                       │
  ├── lint ──────────────────────────────┐         │
  └── unit-tests                         │         │
      └── integration-tests              │         │
          └── docker-build ◄─────────────┘         │
              └── docker-push                      │
                  └── deploy-agent-engine ◄─────────┘
                      └── deploy-cloud-run (100% traffic)
                          └── get-service-url
                              └── smoke-test
                                  └── load-test
                                      └── post-deploy-eval
```

### release.yaml (tag v*.*.*, prod shadow + canary)

```
install-deps
  ├── lint ──────────────────────────────────────┐
  └── unit-tests                                 │
      └── integration-tests                      │
          └── docker-build ◄────────────────────┘
              └── docker-push
                  ├── capture-champion  (read current live revision)
                  ├── get-project-number
                  └── deploy-agent-engine  (NEW resource, display name = tag)
                      └── deploy-cloud-run  (--no-traffic --tag sha-SHORT_SHA)
                          └── get-shadow-url
                              └── smoke-test  (shadow URL, zero prod users)
                                  └── post-deploy-eval
                                      └── enable-canary  (split traffic if eval passes)
                                          └── cleanup-old-engines  (keep 2 most recent)
```

### cloudbuild-nightly.yaml (regression monitor + canary decision)

```
install-deps ──────────┐
resolve-engine-id ─────┴── post-deploy-eval  (eval agent engine, capture exit code)
                               └── promote-or-rollback
                                     pass N nights: promote canary to 100%, reset counter
                                     regression:    rollback to champion, reset counter, fail build
```

`resolve-engine-id` reads `AGENT_ENGINE_RESOURCE_NAME` directly from the active Cloud Run revision's env vars: canary revision if one is live (`sha-*` tag, partial traffic), otherwise the champion. This is always accurate — the env var is the engine wired to that revision, and it self-corrects after rollback without any manual cleanup.

---

## Triggers

### App triggers

| Trigger | Event | Environment | Config | `_EVAL_PROFILE` | Integration tests |
|---------|-------|-------------|--------|-----------------|------------------|
| `ci-pull-request` | PR to `main` | read-only | `pr-checks.yaml` | auto (`fast`/`standard`) | Auto when `customer_support_mas/` changed |
| `ci-cd-push-main` | Push to `main` | dev | `cloudbuild-deploy.yaml` | `standard` | Yes (when relevant files changed) |
| `release-staging` | Tag `v*.*.*-rc.*` | staging | `release-staging.yaml` | `standard` | Yes (always) |
| `release` | Tag `v*.*.*` | prod | `release.yaml` | `standard` | Yes (always) |
| `ci-manual` | Manual or nightly | prod | `cloudbuild-nightly.yaml` | `full` | No |
| Cloud Scheduler | Midnight UTC | prod | `cloudbuild-nightly.yaml` | `full` | No |

### Terraform triggers (run in parallel with app triggers)

| Trigger | Event | Config |
|---------|-------|--------|
| `terraform-plan` | PR to `main` | `cloudbuild/terraform-plan.yaml` |
| `terraform-apply` | Push to `main` | `cloudbuild/terraform-apply.yaml` |

---

## PR Change Detection

`pr-checks.yaml` runs a `detect-agent-changes` step that diffs the PR branch against `origin/main`. Based on what changed, it sets two files that downstream steps read:

| Files changed in PR | `eval_profile.txt` | `run_agent_tests.txt` | Result |
|---------------------|--------------------|-----------------------|--------|
| `customer_support_mas/` touched | `standard` | `true` | Full CI: tool-tests + unit-tests (standard) + integration-tests |
| Only docs, terraform, scripts | `fast` | `false` | Lite CI: tool-tests + unit-tests (fast, Rouge-1 only) |

Integration tests run automatically on any PR that touches agent logic. No manual trigger required.

---

## Event to Trigger Mapping

| Event | Triggers that fire |
|-------|-------------------|
| PR opened/updated against `main` | `ci-pull-request` (auto-detects depth) + `terraform-plan` |
| Push to `main` | `ci-cd-push-main` (dev deploy) + `terraform-apply` |
| Tag `v*.*.*-rc.*` | `release-staging` (staging deploy + eval) |
| Tag `v*.*.*` | `release` (prod shadow deploy + canary enable) |
| Midnight UTC | Cloud Scheduler fires `ci-manual` (regression monitor) |

---

## Production Canary Release

### Creating a release

```bash
# 1. Validate on staging first
git tag v1.2.0-rc.1
git push origin v1.2.0-rc.1
# Wait for release-staging to pass

# 2. Cut the prod release
git tag v1.2.0
git push origin v1.2.0
```

The `release` trigger runs:

1. Lint + unit tests at `standard` eval profile
2. Docker image built and pushed with three tags: `v1.2.0`, `$COMMIT_SHA`, `latest`
3. Champion Cloud Run revision captured (currently live revision)
4. New Agent Engine deployed: display name `customer-support-multiagent-v1.2.0`, new resource ID
5. Cloud Run shadow revision deployed: `--no-traffic --tag sha-SHORT_SHA --revision-suffix sha-SHORT_SHA`
6. Smoke tests run against the shadow revision URL (no prod traffic affected)
7. Post-deploy eval runs against the new Agent Engine via `eval_vertex.py`
8. If eval passes: traffic split set to `(100 - CANARY_PCT)%` champion / `CANARY_PCT%` canary (default 10%)
9. If eval fails: build fails, shadow stays at 0% traffic

### Nightly decision gate

Nightly eval runs against the prod Agent Engine and compares scores against the stored GCS baseline:

Regression is measured using a **composite weighted score**: Tool Use Quality (40%) + Final Response Quality (40%) + Hallucination (10%) + Safety (10%). A single-metric swing from LLM judge variance does not trigger a rollback — only a drop in the overall composite score exceeds the threshold.

- **No regression detected**: baseline updated, nightly passes.
- **Regression detected (composite score drop > `_REGRESSION_THRESHOLD`, default 10%)**: nightly exits 1, team alerted via Cloud Build failure notification.

Canary promotion and rollback happen **automatically**:

| Outcome | Action |
|---------|--------|
| Eval passes, canary live, pass count < threshold | Increment pass counter in GCS, wait for next night |
| Eval passes, canary live, pass count >= threshold | Promote canary to 100%, reset counter |
| Eval passes, no canary | Update baseline, pass |
| Regression detected, canary live | Roll back champion to 100%, reset counter, exit 1 |
| Regression detected, no canary | Reset counter, exit 1 |

The promotion threshold is `_CANARY_PROMOTE_THRESHOLD` (default: 1 consecutive passing night).

Pass counter stored at: `$_STAGING_BUCKET/baselines/canary-pass-count.json`

**Manual overrides** (emergency use):

```bash
# Force promote canary to 100%
gcloud run services update-traffic customer-support-app \
  --to-revisions=CANARY_REVISION=100 \
  --region=us-central1 --project=YOUR_PROD_PROJECT

# Force rollback to champion
gcloud run services update-traffic customer-support-app \
  --to-revisions=CHAMPION_REVISION=100 \
  --region=us-central1 --project=YOUR_PROD_PROJECT
# No env var update needed — nightly reads the engine from the champion revision directly

# Check current traffic split
gcloud run services describe customer-support-app \
  --region=us-central1 --project=YOUR_PROD_PROJECT \
  --format="value(status.traffic)"
```

---

## Staging: Pre-Prod Eval Gate

Staging (`your-project-staging`) uses rc tags to validate the release candidate before the prod tag:

```bash
git tag v1.2.0-rc.1
git push origin v1.2.0-rc.1
```

`release-staging.yaml` runs: full deploy + load tests + post-deploy eval. No shadow (staging can absorb full rollout). If it passes, the prod tag is safe to cut.

---

## Agent Engine Versioning

Every production git tag creates a **new** Agent Engine resource:

| Tag | Agent Engine | Cloud Run status |
|-----|-------------|-----------------|
| `v1.0.0` | `reasoningEngines/111` | decommissioned (old champion) |
| `v1.1.0` | `reasoningEngines/222` | champion (90% traffic) |
| `v1.2.0` | `reasoningEngines/333` | canary (10% traffic) |

**Full version traceability:**

| Artifact | Version identifier |
|----------|--------------------|
| Git source | Tag `v1.2.0` + commit SHA |
| Docker image | `customer-support-app:v1.2.0` in Artifact Registry |
| Agent Engine | `reasoningEngines/333` with display name `customer-support-multiagent-v1.2.0` |
| Cloud Run | Image pinned to `v1.2.0`, revision tagged `sha-SHORT_SHA` |

---

## Terraform CI/CD

Infrastructure changes are managed separately from app code.

### How it works

1. Modify a resource in `terraform/modules/core/` (shared across all envs)
2. Open a PR against `main`: `terraform-plan` fires and posts the diff to build logs
3. Merge the PR: `terraform-apply` fires and applies changes to dev
4. Run `make infra-up ENV=staging` and `make infra-up ENV=prod` to promote to other environments

### Remote state

Terraform state is stored in GCS:

```
gs://{project_id}-tf-state/customer-support-mas/{env}/   ← state files
gs://{project_id}-tf-state/tfvars/terraform.tfvars       ← env config (gitignored locally)
```

Bootstrap the state bucket once per environment:

```bash
make bootstrap-tfstate ENV=dev
make bootstrap-tfstate ENV=staging
make bootstrap-tfstate ENV=prod
```

After any local tfvars change, sync to GCS so CI picks it up:

```bash
make sync-tfvars ENV=dev
make sync-tfvars ENV=staging
make sync-tfvars ENV=prod
```

> **Important:** Always sync tfvars before pushing a PR that touches `terraform/`. The `terraform-apply` CI trigger reads tfvars from GCS — if GCS is stale, the apply will use outdated values and can destroy/recreate resources incorrectly.

### Substitution variables

| Variable | Description | Example |
|----------|-------------|---------|
| `_ENV_DIRECTORY` | Path to environment dir | `terraform/environments/dev` |
| `_ENVIRONMENT` | Environment name | `dev` |
| `_TF_STATE_BUCKET` | GCS bucket for state + tfvars | `YOUR_PROJECT_ID-tf-state` |

---

## Auto-detection of Changes

`cloudbuild-deploy.yaml` runs a `detect-agent-changes` step on every push to `main`. It writes three flags based on `git diff HEAD~1 HEAD`:

**`run_tests.txt`** — controls whether tests run:
- `true` if `customer_support_mas/`, `tests/`, or `eval_wrappers/` changed
- `false` otherwise — tool-tests, unit-tests, and integration-tests are all skipped

**`deploy_agent_engine.txt`** — controls whether Agent Engine is redeployed:
- `true` if `customer_support_mas/` changed (or `_DEPLOY_AGENT_ENGINE=true` is forced)
- `false` otherwise — Agent Engine deploy is skipped

**`run_deploy.txt`** — controls whether Docker build and Cloud Run deploy run:
- `true` if `customer_support_mas/`, `backend/`, `frontend/`, `pyproject.toml`, or `uv.lock` changed
- `false` otherwise — docker-build, docker-push, deploy-cloud-run, smoke-test are all skipped

| Files changed | Tests | Agent Engine | Docker + Cloud Run |
|---------------|-------|--------------|-------------------|
| `customer_support_mas/` | Yes | Yes | Yes |
| `backend/` or `frontend/` | No | No | Yes |
| `tests/` or `eval_wrappers/` | Yes | No | No |
| `terraform/`, `docs/`, `scripts/` | No | No | No |

Force Agent Engine redeploy regardless of changed files:

```bash
make submit-build DEPLOY_AGENT_ENGINE=true
```

---

## Smoke Tests

Smoke tests run at the end of every deployment in all environments. In prod releases they run against the shadow revision URL before any traffic is switched.

**6 checks in `tests/smoke/test_smoke.py`:**

1. Health endpoint returns 200
2. Agent responds to a basic message
3. Product search tool is reachable
4. Order tracking tool is reachable
5. Model Armor filters unsafe prompts
6. Session continuity (follow-up messages use the same session)

**Running locally:**

```bash
uv sync --group dev
CLOUD_RUN_URL=https://your-cloud-run-url pytest tests/smoke/ -v
```

---

## Load Tests

Load tests run in staging only (rc tag releases).

**Tool:** [Locust](https://locust.io/): `tests/load/locustfile.py`

**Configuration:** 5 concurrent users, 2-minute run, `/health` endpoint only (infrastructure availability, not LLM quality)

**SLO thresholds** (`tests/load/check_slos.py`):

| SLO | Threshold |
|-----|-----------|
| p95 response time | < 10 s |
| p99 response time | < 20 s |
| Error rate | < 5 % |
| Requests per second | > 0.5 |

---

## Eval Profiles

| Profile | Unit Metrics | Integration Metrics | Cost |
|---------|-------------|---------------------|------|
| `fast` | Rouge-1 response match | Rouge-1 response match | Free |
| `standard` | + tool name F1 (custom metric) | + rubric-based LLM judge | Low |
| `full` | + LLM-as-judge response quality | + LLM-as-judge response quality | Higher |

Profile configs: `tests/eval_configs/{unit,integration}/{fast,standard,full}.json`

Post-deploy eval configs: `tests/eval_configs/post_deploy/{standard,full}.json`

---

## CI Steps

### 1. install-deps
Installs Python dependencies via `uv sync --frozen` into `/workspace/.venv` (shared across all steps). Each step activates with `export PATH="/workspace/.venv/bin:$PATH"`.

### 2. lint
Runs `ruff check customer_support_mas/ --ignore=E501`. PR checks also run `ruff format --check`.

### 3. tool-tests
Pure Python tests with mocked Firestore: no LLM calls, no cost.
```
pytest tests/unit/test_tools.py tests/unit/test_mock_rag.py tests/unit/test_refund_standalone.py
```
Skipped on push-to-main if no changes in `customer_support_mas/`, `tests/`, or `eval_wrappers/`.

### 4. unit-tests
Agent evaluation via ADK `AgentEvaluator`: calls Vertex AI Gemini.
```
pytest tests/unit/test_agent_eval_ci.py
```
Skipped on push-to-main if no relevant files changed.

### 5. integration-tests
Multi-agent orchestration evaluation through the root agent.
```
pytest tests/integration/test_integration_eval_ci.py
```
- On PRs: runs automatically when `customer_support_mas/` files are in the diff (standard profile).
- On push to main: runs when `customer_support_mas/`, `tests/`, or `eval_wrappers/` changed.
- On staging and prod release tags: always runs (required gate before Docker build).

---

## CD Steps (cloudbuild-deploy.yaml, push to main / dev)

### 6. docker-build
Multi-stage Docker build (`backend/Dockerfile`): React frontend (Node 20) + FastAPI backend (Python 3.11). Tagged with `$COMMIT_SHA` and `latest`.

### 7. docker-push
Pushes to Artifact Registry at `$_REGION-docker.pkg.dev/$PROJECT_ID/customer-support/customer-support-app`.

### 8. deploy-agent-engine (conditional)
Runs `deployment/deploy.py --action deploy` when `_DEPLOY_AGENT_ENGINE=true`. Uses update-or-create logic (same display name for dev).

### 9. deploy-cloud-run
Dev: deploys with 100% traffic to new revision. Prod (release.yaml): shadow with `--no-traffic --tag sha-SHORT_SHA`.

### 10. smoke-test
Runs `pytest tests/smoke/test_smoke.py` against the deployed (or shadow) service URL.

### 11. load-test
Runs Locust for 2 minutes, then `tests/load/check_slos.py` to assert SLO thresholds.

---

## Release CD Steps (release.yaml only)

### 12. capture-champion
Reads the currently live Cloud Run revision name. Written to `/workspace/champion_revision.txt` for use by `enable-canary`.

### 13. deploy-agent-engine
Always creates a new Agent Engine resource with `AGENT_ENGINE_DISPLAY_NAME=customer-support-multiagent-$TAG_NAME`. Resource name written to `/workspace/agent_engine_resource_name.txt`.

### 14. deploy-cloud-run (shadow)
Deploys with `--no-traffic --tag sha-SHORT_SHA --revision-suffix sha-SHORT_SHA`. Shadow revision points to the new Agent Engine via `AGENT_ENGINE_RESOURCE_NAME` env var.

### 15. get-shadow-url
Gets the tagged revision URL from Cloud Run service describe. Written to `/workspace/shadow_url.txt`.

### 16. smoke-test (shadow)
Runs smoke tests against the shadow URL. No prod users affected.

### 17. post-deploy-eval
Runs `tests/eval_vertex.py --agent-engine-id NEW_ENGINE --custom-inference --output /workspace/eval_scores.json`. Exits 0 if thresholds pass, 1 if they fail (blocks enable-canary).

### 18. enable-canary
Runs only if post-deploy-eval passed. Splits Cloud Run traffic between champion and canary revisions using `_CANARY_TRAFFIC_PERCENT` (default 10%). On first deploy (no champion): promotes canary to 100%.

---

## Nightly Pipeline (cloudbuild-nightly.yaml)

Resolves the Agent Engine from the active Cloud Run revision (canary if live, champion otherwise) and compares a composite weighted score against a GCS-stored baseline:

- **First run**: saves current scores as baseline, passes.
- **No regression**: updates baseline, passes.
- **Regression detected (composite score drop > `_REGRESSION_THRESHOLD`, default 0.10)**: exits 1.

The nightly baseline is stored at `$_STAGING_BUCKET/baselines/nightly-baseline.json`.

```bash
# Run manually
gcloud builds triggers run ci-manual \
  --project=YOUR_PROJECT_ID --region=us-central1 --branch=main
```

---

## Setup

### Quick Start (Terraform: recommended)

```bash
# 1. Copy and fill in your values for each environment
cp terraform/environments/dev/terraform.tfvars.example \
   terraform/environments/dev/terraform.tfvars
$EDITOR terraform/environments/dev/terraform.tfvars

# 2. Create GCS state bucket (once per env)
make bootstrap-tfstate ENV=dev

# 3. Bootstrap infrastructure
make infra-up ENV=dev

# 4. Connect GitHub repo (one-time, browser OAuth)
#    Cloud Console → Cloud Build → Repositories (2nd gen) → Create host connection → GitHub
#    Then: Link Repository → select your repo
#    Set github_connected=true, cloudbuild_connection_name, cloudbuild_repo_name in terraform.tfvars
#    Then: make sync-tfvars ENV=dev && make infra-up ENV=dev

# 5. Seed Firestore and deploy
make seed-db
make deploy-agent-engine
make deploy-cloud-run
```

See [../terraform/](../terraform/) for full Terraform configuration and [DEPLOYMENT.md](./DEPLOYMENT.md) for the complete multi-environment setup walkthrough.

### Branch Protection Rules

Protect `main` so a failing check blocks the merge button:

**GitHub → repo → Settings → Branches → Add branch protection rule** for `main`:

| Setting | Value |
|---------|-------|
| Branch name pattern | `main` |
| Require status checks to pass | enabled |
| Required checks | `ci-pull-request`, `terraform-plan` |
| Require branches to be up to date | enabled |
| Do not allow bypassing | enabled |

### IAM Roles Required

The Cloud Build service account (`PROJECT_NUMBER@cloudbuild.gserviceaccount.com`) needs:

| Role | Purpose |
|------|---------|
| `roles/datastore.user` | Firestore access |
| `roles/aiplatform.user` | Vertex AI Gemini API |
| `roles/aiplatform.admin` | Agent Engine deployment |
| `roles/artifactregistry.writer` | Push Docker images |
| `roles/run.admin` | Cloud Run deployment + traffic management |
| `roles/iam.serviceAccountUser` | Act as Cloud Run service account |
| `roles/storage.objectAdmin` | Staging bucket + tfstate bucket + baseline JSON |
| `roles/editor` (or targeted) | `terraform-apply`: create/update GCP resources |

All roles are granted by Terraform (`terraform/modules/core/iam.tf`). No service account key file is needed.

### Creating Triggers

> **Prerequisites**
> 1. Create a 2nd gen host connection: Cloud Console → Cloud Build → **Repositories (2nd gen)** → **Create host connection** → GitHub → name it `github-connection`
> 2. Link the repository: **Link Repository** → select `YOUR_GITHUB_USERNAME/adk-multiagent-production-template`
> 3. Get the slugified repo name: `gcloud builds repositories list --connection=github-connection --region=us-central1`
> 4. Set `github_connected=true`, `cloudbuild_connection_name`, and `cloudbuild_repo_name` in `terraform/environments/*/terraform.tfvars`, then run `make infra-up`
>
> **Important:** Cloud Build 2nd gen triggers require `service_account` in the API request. Terraform handles this automatically.

#### Cloud Console: quick reference

| Field | ci-pull-request | ci-cd-push-main | release-staging | release | ci-manual |
|-------|----------------|-----------------|-----------------|---------|-----------|
| **Name** | `ci-pull-request` | `ci-cd-push-main` | `release-staging` | `release` | `ci-manual` |
| **Event** | Pull request | Push to branch | Push tag | Push tag | Manual |
| **Branch / Tag** | `^main$` | `^main$` | `^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+` | `^v[0-9]+\.[0-9]+\.[0-9]+$` | `main` |
| **Build config** | `cloudbuild/pr-checks.yaml` | `cloudbuild/cloudbuild-deploy.yaml` | `cloudbuild/release-staging.yaml` | `cloudbuild/release.yaml` | `cloudbuild/cloudbuild-nightly.yaml` |
| **_STAGING_BUCKET** | | `gs://YOUR_BUCKET` | `gs://YOUR_BUCKET` | `gs://YOUR_BUCKET` | `gs://YOUR_BUCKET` |
| **_AGENT_ENGINE_RESOURCE_NAME** | | `projects/.../reasoningEngines/ID` | | `projects/.../reasoningEngines/ID` | `projects/.../reasoningEngines/ID` |
| **_CANARY_TRAFFIC_PERCENT** | | | | `10` | |
| **_EVAL_DATASET** | | | `tests/post_deploy/datasets/post_deploy_cases.json` | `tests/post_deploy/datasets/post_deploy_cases.json` | |

#### Trigger: Push to `main` (dev, CI + CD)

```bash
gcloud builds triggers import --region=us-central1 --project=YOUR_PROJECT_ID --source=- <<'EOF'
name: ci-cd-push-main
filename: cloudbuild/cloudbuild-deploy.yaml
repositoryEventConfig:
  push:
    branch: "^main$"
  repository: projects/YOUR_PROJECT_ID/locations/us-central1/connections/github-connection/repositories/YOUR_GITHUB_USERNAME-adk-multiagent-production-template
  repositoryType: GITHUB
serviceAccount: projects/YOUR_PROJECT_ID/serviceAccounts/YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com
substitutions:
  _GOOGLE_CLOUD_LOCATION: us-central1
  _DEPLOY_AGENT_ENGINE: "false"
  _STAGING_BUCKET: gs://YOUR_DEV_STAGING_BUCKET
  _AGENT_ENGINE_RESOURCE_NAME: ""
EOF
```

#### Trigger: Release tag (prod canary)

```bash
gcloud builds triggers import --region=us-central1 --project=YOUR_PROJECT_ID --source=- <<'EOF'
name: release
filename: cloudbuild/release.yaml
repositoryEventConfig:
  push:
    tag: "^v[0-9]+\\.[0-9]+\\.[0-9]+$"
  repository: projects/YOUR_PROJECT_ID/locations/us-central1/connections/github-connection/repositories/YOUR_GITHUB_USERNAME-adk-multiagent-production-template
  repositoryType: GITHUB
serviceAccount: projects/YOUR_PROJECT_ID/serviceAccounts/YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com
substitutions:
  _EVAL_PROFILE: standard
  _GOOGLE_CLOUD_LOCATION: us-central1
  _STAGING_BUCKET: gs://YOUR_STAGING_BUCKET
  _AGENT_ENGINE_RESOURCE_NAME: ""
  _CANARY_TRAFFIC_PERCENT: "10"
EOF
```

#### Trigger: Nightly regression monitor (prod)

Created automatically by Terraform. Run manually:

```bash
gcloud builds triggers run ci-manual --region=us-central1 --project=YOUR_PROJECT_ID --branch=main
```

#### Fixing existing triggers with typos

```bash
# List trigger IDs
gcloud builds triggers list --region=us-central1 --format="table(name,id)"

# Re-import with the id field to update in place
gcloud builds triggers import --region=us-central1 --project=YOUR_PROJECT_ID --source=- <<'EOF'
name: ci-cd-push-main
id: YOUR_TRIGGER_ID
filename: cloudbuild/cloudbuild-deploy.yaml
...
EOF
```

### Nightly Schedule

Cloud Scheduler (created automatically by Terraform for prod):

```bash
TRIGGER_ID=$(gcloud builds triggers list \
  --filter="name=ci-manual" --format="value(id)")

gcloud scheduler jobs create http nightly-full-eval \
  --schedule="0 0 * * *" \
  --uri="https://cloudbuild.googleapis.com/v1/projects/$PROJECT_ID/locations/us-central1/triggers/${TRIGGER_ID}:run" \
  --http-method=POST \
  --oauth-service-account-email="PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --message-body='{}' \
  --time-zone="UTC"
```

---

## Substitution Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `_EVAL_PROFILE` | `standard` | Eval metric profile (`fast`, `standard`, `full`) |
| `_PYTHON_VERSION` | `3.11` | Python version for test containers |
| `_FIRESTORE_DATABASE` | `customer-support-db` | Firestore database name |
| `_GOOGLE_CLOUD_LOCATION` | `us-central1` | GCP region |
| `_REGION` | `us-central1` | Cloud Run / Artifact Registry region |
| `_SERVICE_NAME` | `customer-support-app` | Cloud Run service name |
| `_AR_REPO` | `customer-support` | Artifact Registry repository |
| `_DEPLOY_AGENT_ENGINE` | `false` | Force Agent Engine redeploy on push to main (auto-detected from git diff) |
| `_STAGING_BUCKET` | | GCS staging bucket (e.g. `gs://my-bucket`) |
| `_AGENT_ENGINE_RESOURCE_NAME` | | Full resource name of the current prod Agent Engine |
| `_MODEL_ARMOR_ENABLED` | `false` | Enable Model Armor prompt filtering |
| `_MODEL_ARMOR_TEMPLATE_ID` | | Model Armor template ID |
| `_RUN_LOAD_TESTS` | `false` | Run Locust load tests (staging rc tags only) |
| `_CANARY_TRAFFIC_PERCENT` | `10` | % of prod traffic to send to canary after eval passes |
| `_CANARY_PROMOTE_THRESHOLD` | `1` | Consecutive passing nights before canary is auto-promoted (nightly) |
| `_REGRESSION_THRESHOLD` | `0.10` | Max allowed composite score drop vs baseline, e.g. 0.10 = 10% (nightly) |
| `_EVAL_DATASET` | `tests/post_deploy/datasets/post_deploy_cases.json` | Dataset for post-deploy eval (staging + prod release) |

`$PROJECT_ID` and `$COMMIT_SHA` are built-in Cloud Build substitutions.

---

## Timeouts

| Pipeline | Timeout | Rationale |
|----------|---------|-----------|
| `pr-checks.yaml` | 40 min | Up to standard profile + integration tests when agent files changed |
| `cloudbuild-deploy.yaml` | 60 min | CI + Docker + Agent Engine + Cloud Run + smoke |
| `release-staging.yaml` | 90 min | Full CI + Docker + Agent Engine + load tests + eval |
| `release.yaml` | 90 min | Full CI + Docker + new Agent Engine + shadow + eval + canary |
| `cloudbuild-nightly.yaml` | 60 min | Full eval with LLM judges + baseline comparison + promote-or-rollback |
| `terraform-plan.yaml` | 20 min | Init + plan only |
| `terraform-apply.yaml` | 30 min | Init + apply |

---

## Make Targets for CI

```bash
make install          # pip install + pre-commit install
make lint             # ruff check + ruff format --check
make format           # auto-fix formatting
make test-tools       # pure Python tests, no LLM
make test-unit        # unit agent eval (EVAL_PROFILE=fast by default)
make test-integration # integration eval (EVAL_PROFILE=fast by default)
make test             # run all three in sequence

# Override eval profile
make test-unit EVAL_PROFILE=standard
make test-unit EVAL_PROFILE=full

# Post-deploy eval against a deployed Agent Engine
make eval-post-deploy AGENT_ENGINE_ID=<id> EVAL_PROFILE=standard

# Submit the full CI+CD pipeline to Cloud Build (dev)
make submit-build                          # Cloud Run only
make submit-build DEPLOY_AGENT_ENGINE=true # + Agent Engine redeploy
make submit-build EVAL_PROFILE=fast        # faster feedback

# Trigger the nightly pipeline against code on main
git push origin main
make nightly
make nightly RUN_POST_DEPLOY_EVAL=true

# Show all targets
make help
```

---

## Skipping Builds with `[skip ci]`

For commits that change nothing deployable (docs, CI config):

```bash
git commit -m "docs: update CI_CD.md [skip ci]"
```

**When to use each mechanism:**

| Scenario | Mechanism |
|----------|-----------|
| Feature branch work | Push normally, PR fires fast CI |
| Agent logic changed on PR | Integration tests run automatically |
| Merging to `main` | Push normally, auto-deploys to dev |
| Staging validation | Tag `v*.*.*-rc.*`, push tag |
| Production release | Tag `v*.*.*`, push tag |
| Docs or CI config only | Add `[skip ci]` to commit message |
