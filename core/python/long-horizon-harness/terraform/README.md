# Terraform — the deployed stack

This directory provisions everything the deployed agent runs on: Cloud SQL,
both Cloud Run services, IAM, Secret Manager, Cloud Scheduler, IAP, and the
GCS buckets.

**Do not run `terraform apply` here by hand.** The root `Makefile` owns the
invocation and passes the variables the config requires:

```bash
make deploy  PROJECT_ID=<project>   # terraform apply, then backend + web images
make destroy PROJECT_ID=<project>   # tear down every billable resource
```

`make deploy` runs Terraform first, then rolls the real images over the two
Cloud Run skeletons (both `ignore_changes` the image, so they don't fight
Terraform). See the root `README.md` `## Deploy` section for the full flow.

| File | Provisions |
|---|---|
| `main.tf` | provider + required API enablement |
| `cloud_sql.tf` | Postgres instance, database, user, password secret |
| `cloud_run.tf` / `cloud_run_web.tf` | the `lha` backend and `lha-web` proxy |
| `iam.tf` / `secrets.tf` | service accounts, custom roles, per-user secret role |
| `cloud_scheduler.tf` | dream-review / snapshot / routine ticks |
| `agent_engine.tf` · `artifact_bucket.tf` · `gcp_oauth.tf` | Memory Bank engine, artifact bucket, OAuth wiring |
| `skills_bucket.tf` | orphan; see "Skills bucket" below |

## Skills bucket (orphan)

> **Status:** unused. Workspace state now lives in Vertex Agent Runtime sandboxes
> (snapshots stored under the parent `reasoningEngine`), so this bucket is no
> longer wired into the runtime. Retained as a stub for future shared-state
> needs (e.g. a multi-host snapshot index).

It creates `google_storage_bucket.skills` (`lha-skills-<env>`, US multi-region,
STANDARD, uniform bucket-level IAM, public access blocked), versioning on, and
two lifecycle rules: keep ≤5 noncurrent versions, delete noncurrent versions
older than 30 days. If you wire it back in, grant the runtime SA
`roles/storage.objectAdmin` on it from the deployment terraform — this module
shouldn't know who its consumers are.

## Sandbox backend

Per-user workspace persistence is provided by `SandboxEnvironment`
(`horizon/environment/sandbox.py`). A session **reattaches** to the user's
most-recent RUNNING sandbox if there is one, else provisions blank (or restores
a snapshot when Phase C is on). `close()` only tears down the local HTTP client
— **it does not snapshot**; the platform-side sandbox keeps running for the next
session. Full lifecycle: [`../docs/sandbox-lifecycle.md`](../docs/sandbox-lifecycle.md).
Selection is controlled by:

```bash
# Required to switch off LocalEnvironment.
export LHA_ENVIRONMENT_BACKEND=sandbox

# Optional: pins the Agent Engine hosting Memory Bank + sandboxes. Unset, it is
# discovered/created by scripts/provision_agent_engine.py on `terraform apply`.
export AGENT_ENGINE_RESOURCE_NAME=projects/<p>/locations/<l>/reasoningEngines/<r>

# Required when backend=sandbox. Missing either of the two below, the provider
# logs a warning and falls back to LocalEnvironment off Cloud Run (and raises
# SandboxConfigurationError on it).
#
# BYOC runtime image the sandbox boots; rebuilt + pushed from
# horizon/sandbox/runtime/. A new tag forces a new template. Nothing in this
# module creates the Artifact Registry repo — see ../AGENTS.md for the one-time
# `gcloud artifacts repositories create` + Cloud Build steps.
export LHA_RUNTIME_IMAGE=us-central1-docker.pkg.dev/<p>/lha-sandbox/runtime:<tag>

# SA email whose JWT the sandbox LB checks on every shim request.
# Caller ADC must hold roles/iam.serviceAccountTokenCreator on this SA.
# `make deploy` reuses the Cloud Run SA for this; locally you create your own.
export LHA_SANDBOX_CALLER_SA=lha-sandbox-caller@<p>.iam.gserviceaccount.com
```

Sandbox discovery is Agent Platform's authoritative `sandboxes.list`, not a
host-local index file — so any Cloud Run instance resolves the same sandbox and
a process restart is irrelevant to reattach.

## Redeploying into a project you previously tore down

GCP soft-deletes custom project IAM roles and blocks recreating the same
`role_id` for about a week, so a `make deploy` within that window fails on
`lha_user_secrets` with `FAILED_PRECONDITION` ("marked for deletion"). The
soft-deleted role is not visible to `gcloud iam roles describe/list
--show-deleted`. Either wait out the window or change `role_id` in
`secrets.tf`.

## IAP access

The Cloud Run footprint is two services:

- `lha` (backend) — `iap_enabled = false`. Invoked only by the
  `lha-web-run` SA (web frontend), the scheduler SA, and A2A peers.
  Verifies identity itself (`LHA_AUTH_MODE=iap`): the IAP JWT forwarded by
  `lha-web` as `X-LHA-IAP-Assertion`, or an end-user OAuth bearer on A2A calls
  from Gemini Enterprise.
- `lha-web` (frontend) — `iap_enabled = true`. The Express proxy in
  `web/server/` validates the IAP JWT against
  `/projects/<PROJECT_NUMBER>/locations/<REGION>/services/lha-web` and
  forwards the verified email to the backend.

Grant end-users access via the `iap_users` Terraform variable:

```bash
terraform apply -var 'iap_users=["user:you@example.com","group:eng@example.com"]'
```

For local dev, set `LHA_AUTH_MODE=dev` and `LHA_DEV_USER_ID=<email>` in
`.env` to bypass IAP and pin a stable identity.
