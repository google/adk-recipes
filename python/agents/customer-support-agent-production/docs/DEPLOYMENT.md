# Deployment Guide

End-to-end setup for a new user starting from a fresh clone.

## Prerequisites

Install these tools before starting:

| Tool | Version | Install |
|------|---------|---------|
| `gcloud` CLI | latest | [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install) |
| Terraform | >= 1.5 | [developer.hashicorp.com/terraform](https://developer.hashicorp.com/terraform/install) |
| Python | 3.11 | [python.org](https://www.python.org/downloads/) |
| `uv` | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20+ | [nodejs.org](https://nodejs.org/) (local dev only) |

---

## Step 1: Authenticate and clone

```bash
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID

git clone https://github.com/Saoussen-CH/adk-multiagent-production-template.git
cd adk-multiagent-production-template
```

---

## Step 2: Configure .env

```bash
cp .env.example .env
```

Fill in `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `FIRESTORE_DATABASE`.
Leave `GOOGLE_CLOUD_STORAGE_BUCKET`, `AGENT_ENGINE_RESOURCE_NAME`, and
`MODEL_ARMOR_TEMPLATE_ID` blank for now: they come from Terraform output.

See [ENV_SETUP.md](./ENV_SETUP.md) for full variable reference.

---

## Step 3: Bootstrap GCP project

The project uses a **multi-environment Terraform layout**: one GCP project per environment (dev/staging/prod), each with its own state and config. All infrastructure is defined once in `terraform/modules/core/` and deployed to each environment independently.

**Enable the Cloud Resource Manager API first** — Terraform requires it but cannot enable it itself:

```bash
gcloud services enable cloudresourcemanager.googleapis.com --project=YOUR_DEV_PROJECT_ID
gcloud services enable cloudresourcemanager.googleapis.com --project=YOUR_STAGING_PROJECT_ID
gcloud services enable cloudresourcemanager.googleapis.com --project=YOUR_PROD_PROJECT_ID
```

```bash
cp terraform/environments/dev/terraform.tfvars.example \
   terraform/environments/dev/terraform.tfvars
```

Edit the three required fields:

```hcl
project_id          = "your-dev-project-id"
staging_bucket_name = "your-dev-project-id-staging"   # must be globally unique
github_owner        = "your-github-username"
```

**Create the GCS state bucket and apply infrastructure** (once per environment):

```bash
# Dev
make bootstrap-tfstate ENV=dev   # creates bucket + enables Resource Manager API + grants your account access + uploads tfvars
make infra-up ENV=dev

# Staging
make bootstrap-tfstate ENV=staging
make infra-up ENV=staging

# Prod
make bootstrap-tfstate ENV=prod
make infra-up ENV=prod
```

`bootstrap-tfstate` automatically:
1. Enables `cloudresourcemanager.googleapis.com` (required by Terraform)
2. Creates the GCS state bucket
3. Grants your active `gcloud` account `storage.admin` so `terraform init` can read remote state immediately
4. Uploads `terraform.tfvars` to GCS (Cloud Build reads it from there)

> **Note:** The Compute Engine service account (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`) does not exist until after the first `make infra-up` — it is created when GCP enables the Compute Engine API. Do not try to grant it bucket access before running `infra-up`. Terraform handles that IAM binding automatically on first apply.

Terraform creates:
- GCS staging bucket (Agent Engine artifacts)
- Artifact Registry repository
- Firestore database + vector index
- Cloud Build triggers (app CI/CD + terraform plan/apply)
- All IAM bindings (Cloud Run SA, Agent Engine SA, Cloud Build SA)
- Model Armor template + floor settings
- Cloud Scheduler nightly job (prod only)

> After any local tfvars change (e.g. adding `agent_engine_resource_name` after first deploy),
> sync it back to GCS so CI picks it up:
> ```bash
> make sync-tfvars ENV=dev
> ```

---

## Step 5: Update .env from Terraform outputs

```bash
terraform -chdir=terraform/environments/dev output
```

Copy the values into `.env.dev` (or `.env` for single-env setups):

| Terraform output | .env variable |
|-----------------|---------------|
| `staging_bucket` | `GOOGLE_CLOUD_STORAGE_BUCKET=gs://<value>` |
| `firestore_database_id` | `FIRESTORE_DATABASE=<value>` |
| `model_armor_template_name` | `MODEL_ARMOR_TEMPLATE_ID=<value>` |

---

## Step 6: Install Python dependencies, seed Firestore, add embeddings

```bash
make install
make seed-db          # load demo products, orders, users, invoices
make add-embeddings   # add vector embeddings for RAG semantic search
```

Repeat for **every environment** before running the staging/prod eval pipelines:

```bash
make seed-db ENV=staging
make add-embeddings ENV=staging

make seed-db ENV=prod
make add-embeddings ENV=prod
```

> `add-embeddings` can take a few minutes. The Firestore vector index must be
> READY first: Terraform creates it automatically.

---

## Step 7: Connect GitHub to Cloud Build (2nd gen)

One-time manual step in the GCP Console (cannot be automated):

1. Go to **Cloud Build → Repositories (2nd gen)**
2. Click **Create host connection** → select **GitHub** → authorize → name it `github-connection` (region: `us-central1`)
3. Click **Link Repository** → select `YOUR_GITHUB_USERNAME/adk-multiagent-production-template` → click **Link**
4. Confirm the linked repo name:
   ```bash
   gcloud builds repositories list --connection=github-connection \
     --region=us-central1 --project=YOUR_PROJECT_ID
   ```
   Cloud Build slugifies the name, e.g. `YOUR_GITHUB_USERNAME-adk-multiagent-production-template`

Then enable trigger creation in Terraform:

```hcl
# In terraform/environments/dev/terraform.tfvars (repeat for staging/prod):
github_connected           = true
cloudbuild_connection_name = "github-connection"
cloudbuild_repo_name       = "YOUR_GITHUB_USERNAME-adk-multiagent-production-template"  # from step above
```

```bash
make sync-tfvars ENV=dev   # upload updated tfvars to GCS
make infra-up ENV=dev
```

This creates all CI/CD triggers. **Note:** Cloud Build 2nd gen triggers require
`service_account`: this is set automatically by Terraform to the Cloud Build SA.

---

## Step 8: Deploy Agent Engine (first time)

```bash
make deploy-agent-engine
```

This creates the Agent Engine on Vertex AI. At the end it prints the resource
name: copy it into both `.env` and `terraform/environments/prod/terraform.tfvars`:

**.env:**
```bash
AGENT_ENGINE_RESOURCE_NAME=projects/PROJECT_NUMBER/locations/us-central1/reasoningEngines/ENGINE_ID
```

**terraform/environments/prod/terraform.tfvars:**
```hcl
agent_engine_resource_name = "projects/PROJECT_NUMBER/locations/us-central1/reasoningEngines/ENGINE_ID"
```

Then re-apply Terraform so the Cloud Run service picks up the new value:

```bash
make sync-tfvars ENV=dev   # upload updated tfvars to GCS
make infra-up ENV=dev
```

---

## Step 9: Grant Agent Engine SA permissions

The Agent Engine service account (`gcp-sa-aiplatform-re`) is created by Google
on first Agent Engine deployment: it does not exist before that. Re-run setup
now that it exists, then set `google_managed_sas_exist = true` and re-apply:

```bash
make setup-gcp
```

In the relevant `terraform/environments/*/terraform.tfvars`:

```hcl
google_managed_sas_exist = true
```

```bash
make sync-tfvars ENV=dev   # repeat for staging/prod
make infra-up ENV=dev
```

---

## Step 10: Deploy

Push to `main` and let the CI/CD pipeline handle the deployment:

```bash
git push
```

The Cloud Build trigger fires and runs the full pipeline:

```
install → lint + tool-tests → unit-tests → integration-tests
  → docker-build → docker-push
    → deploy-agent-engine (skipped: no agent code changed)
      → deploy-cloud-run
        → get-service-url
          → smoke-test
```

Alternatively, deploy Cloud Run directly without CI/CD:

```bash
make deploy-cloud-run
```

Get the Cloud Run URL:

```bash
gcloud run services describe customer-support-app \
  --region=us-central1 \
  --format='value(status.url)'
```

---

## Step 11: Verify with smoke tests

```bash
uv sync --group dev
CLOUD_RUN_URL=https://your-cloud-run-url pytest tests/smoke/ -v
```

The smoke suite runs 6 checks: health endpoint, agent responds, product tool,
order tool, Model Armor filtering, and session continuity.

---

## Step 12: Try it

Demo accounts (pre-seeded by `make seed-db`):

| Email | Password | Profile |
|-------|----------|---------|
| `demo@example.com` | `demo123` | Gold tier, has order history |
| `jane@example.com` | `jane123` | Silver tier, has order history |

Try these prompts:
- `Where is my order ORD-12345?`
- `Search for gaming laptops`
- `Ignore all previous instructions...`: blocked by Model Armor

---

## Multi-environment setup

The project supports three environments backed by separate GCP projects and
Terraform state. Each environment has its own directory under
`terraform/environments/`.

| Environment | Directory | Trigger | Model Armor | Load tests |
|-------------|-----------|---------|-------------|------------|
| `dev` | `terraform/environments/dev` | Push to `main` | INSPECT_ONLY | No |
| `staging` | `terraform/environments/staging` | Tag `v*.*.*-rc.*` | INSPECT_AND_BLOCK | Yes |
| `prod` | `terraform/environments/prod` | Tag `v*.*.*` | INSPECT_AND_BLOCK | No |

Per-environment differences:

| Setting | dev | staging | prod |
|---------|-----|---------|------|
| Model Armor mode | INSPECT_ONLY | INSPECT_AND_BLOCK | INSPECT_AND_BLOCK |
| Firestore delete protection | disabled | enabled | enabled |
| GCS force_destroy | true | false | false |
| Nightly scheduler | No | No | Yes |
| Load tests in CI | No | Yes | No |

### Bootstrapping each environment

All three environments follow the same steps. Run from the repo root:

```bash
# 1. Copy and fill in tfvars
cp terraform/environments/dev/terraform.tfvars.example \
   terraform/environments/dev/terraform.tfvars
# Edit: project_id, staging_bucket_name, github_owner

# 2. Create GCS state bucket and upload tfvars (one-time per env)
make bootstrap-tfstate ENV=dev
make sync-tfvars ENV=dev

# 3. Grant ADC account bucket access if it differs from the bucket creator
gsutil iam ch user:YOUR_ADC_ACCOUNT@gmail.com:roles/storage.admin \
  gs://YOUR_DEV_PROJECT_ID-tf-state

# 4. Grant the compute SA bucket access (required for CI terraform-plan/apply to work)
#    The compute SA is created by GCP: get the project number from: gcloud projects describe YOUR_PROJECT_ID
gsutil iam ch serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com:roles/storage.admin \
  gs://YOUR_DEV_PROJECT_ID-tf-state

# 5. Apply infrastructure (after this, Terraform owns the SA permission permanently)
make infra-up ENV=dev
```

Repeat with `ENV=staging` and `ENV=prod`.

> **Important:** Run the full bootstrap sequence for **all three environments
> before merging PRs** through the promotion flow. The `terraform-plan`/`terraform-apply` CI triggers
> require the state bucket to exist and the compute SA to have access before they can run.

Shared infrastructure code lives in `terraform/modules/core/`.

---

## Subsequent deployments

| Scenario | Command |
|----------|---------|
| Agent code changed | `git push`: CI runs + Agent Engine + Cloud Run redeployed |
| Backend/frontend only | `git push`: tests skipped, Docker + Cloud Run redeployed |
| Docs/terraform only | `git push`: tests, Docker, and deploy all skipped |
| **Terraform changes** | Run `make sync-tfvars ENV=dev && make sync-tfvars ENV=staging && make sync-tfvars ENV=prod` before pushing — CI `terraform-apply` reads tfvars from GCS, not from your local file |
| Force Agent Engine redeploy | `make submit-build DEPLOY_AGENT_ENGINE=true` |
| Manual build without push | `make submit-build` |
| Deploy to dev | Push to `main` |
| Deploy to staging | Tag `v*.*.*-rc.*` and push tag |
| Deploy to prod (canary) | Tag `v*.*.*` and push tag |

---

## Useful make targets

```bash
make test                  # run all tests locally (EVAL_PROFILE=fast)
make test-local            # run agent locally before deploying
make test-model-armor      # smoke test Model Armor (safe + unsafe prompts)
make lint                  # ruff check
make format                # ruff auto-fix
make seed-db               # re-seed Firestore
make deploy-agent-engine   # deploy/update Agent Engine only
make deploy-cloud-run      # deploy Cloud Run directly
make bootstrap-tfstate ENV=dev   # create GCS state bucket (once per env)
make sync-tfvars ENV=dev         # upload updated local tfvars to GCS
make infra-up ENV=dev            # terraform init + apply for dev
make infra-up ENV=staging        # terraform init + apply for staging
make infra-up ENV=prod           # terraform init + apply for prod
make terraform-plan ENV=dev      # preview infrastructure changes
```

---

## Creating a release

Once code is merged to `main` and verified, tag the commit to trigger the release pipeline:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The `release` Cloud Build trigger fires automatically on the prod project. It runs lint + unit + integration tests, builds and pushes a Docker image tagged with `v1.0.0` / `$COMMIT_SHA` / `latest`, creates a new Agent Engine with a versioned display name, deploys Cloud Run as a **shadow revision** (zero live traffic), runs smoke tests against the shadow URL, runs post-deploy eval against the new Agent Engine, and if eval passes splits traffic to send `_CANARY_TRAFFIC_PERCENT` (default 10%) to the new revision. The nightly pipeline then auto-promotes the canary after 1 consecutive passing night.

See [CI_CD.md](./CI_CD.md#release-pipeline) for full release pipeline details and the versioning strategy.

### Rollback a release

No rebuild needed: Cloud Run is pinned to the version tag:

```bash
gcloud run deploy customer-support-app \
  --image=us-central1-docker.pkg.dev/YOUR_PROD_PROJECT/customer-support/customer-support-app:v0.0.9 \
  --region=us-central1 \
  --project=YOUR_PROD_PROJECT
```

### Update pyproject.toml version before tagging

Keep the version in `pyproject.toml` in sync with the git tag:

```bash
# Edit pyproject.toml: version = "1.0.0"
git add pyproject.toml
git commit -m "chore: bump version to 1.0.0"
git push origin main
git tag v1.0.0
git push origin v1.0.0
```

---

## See also

- [ARCHITECTURE.md](./ARCHITECTURE.md): system design and components
- [CI_CD.md](./CI_CD.md): CI/CD pipeline details and trigger setup
- [ENV_SETUP.md](./ENV_SETUP.md): full .env variable reference
