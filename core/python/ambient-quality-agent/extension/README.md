# Deploying AQA to Agent Runtime (durable investigations)

The AQA orchestrator runs investigations as durable, long-running query jobs
submitted against itself (`run_query_job` / `:asyncQuery`):
1. The user turn requests an investigation and returns immediately with a
   `pending` run status.
2. The orchestrator runs the investigation inside a platform-managed
   long-running operation (LRO) lasting up to 7 days, surviving beyond the user
   session.
3. The agent receives the self-issued `__RUN_INVESTIGATION__ <run_id>` message,
   executes the investigation graph, and writes results to `app:`-scoped session
   state.

The engine automatically receives its resource ID via
`GOOGLE_CLOUD_AGENT_ENGINE_ID` from Agent Runtime, avoiding two-phase
self-patching.

**This guide covers manual deployment.** When running AQuA alongside an agent
managed by `agents-cli`, install this repository as an extension (`agents-cli
extension add`). The commands `agents-cli infra single-project --apply-aqua` and
`agents-cli deploy --deploy-aqua` automate the steps below. Deployment wrappers are in `acli_infra.py` and
`acli_deploy.py`, and `_acli.py` details their design.

## Prerequisites

- A GCP project with the Gemini platform API (`aiplatform.googleapis.com`)
  enabled and Application Default Credentials configured
  (`gcloud auth application-default login`).
- An Agent Runtime agent created on or after 2026-04-22 (required for long-running
  query jobs; earlier instances cannot run durable jobs).
- A GCS bucket for staging and durable job input and output.

## 1. One-time infra & IAM

The Agent Runtime service agent requires permissions to:
1. Read and write the jobs bucket for durable job input and output.
2. Call `run_query_job` (`roles/aiplatform.user`) on this project so the engine
   can query itself.

```bash
PROJECT=your-gcp-project
PNUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA="service-${PNUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
BUCKET="${PROJECT}-aqua-jobs"

# Create the GCS bucket for staging and durable job I/O.
gcloud storage buckets create "gs://${BUCKET}" \
  --project="$PROJECT" --location=us-central1

# Label the bucket for Cloud Billing cost attribution (see "Cost attribution" below).
# `buckets create` does not take --labels, so apply it in an update call.
gcloud storage buckets update "gs://${BUCKET}" --update-labels=component=aqa

# Grant the service agent read/write permissions for job input and output.
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin"

# Allow the service agent to call run_query_job (reasoningEngines.query/asyncQuery).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/aiplatform.user" --condition=None
```

### BigQuery storage (required)

The agent uses a single BigQuery dataset containing eight tables:
- `insights` and `insight_occurrences`: Read and written by the
  `insight_correlation` node.
- `insight_root_causes`: Stores diagnoses recorded by RCA chat turns for an
  occurrence.
- `investigations`: Stores lifecycle snapshots for each investigation run.
- `investigation_events`: Records progress events emitted by investigation
  workflow nodes.
- `trajectories`: Stores metadata for trajectories sampled during sweeps.
- `trajectory_payloads` and `trajectory_payload_turns`: Archive conversation
  payloads (a manifest row per conversation and turn-level details,
  respectively).

Table schemas are defined in
[`terraform/modules/aqa/schemas`](../terraform/modules/aqa/schemas). The
Terraform files [`insights.tf`](../terraform/modules/aqa/insights.tf),
[`investigations.tf`](../terraform/modules/aqa/investigations.tf),
[`trajectories.tf`](../terraform/modules/aqa/trajectories.tf), and
[`trajectory_payloads.tf`](../terraform/modules/aqa/trajectory_payloads.tf)
provision the dataset and tables. Applying the Terraform module sets
`AQA_DATASET` in the engine environment, making manual table creation
unnecessary.

To create the tables manually (such as for local development), use `bq` with the
schema JSON files:

```bash
bq mk -d --location=us-central1 \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET"

jq '.schema' terraform/modules/aqa/schemas/insights.json > /tmp/insights.json
bq mk --table --schema /tmp/insights.json \
  --clustering_fields agent_name \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.insights"

jq '.schema' terraform/modules/aqa/schemas/insight_occurrences.json > /tmp/occ.json
bq mk --table --schema /tmp/occ.json \
  --clustering_fields insight_id \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.insight_occurrences"

jq '.schema' terraform/modules/aqa/schemas/insight_root_causes.json > /tmp/rc.json
bq mk --table --schema /tmp/rc.json \
  --clustering_fields insight_id \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.insight_root_causes"

jq '.schema' terraform/modules/aqa/schemas/investigations.json > /tmp/inv.json
bq mk --table --schema /tmp/inv.json \
  --clustering_fields run_id \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.investigations"

jq '.schema' terraform/modules/aqa/schemas/investigation_events.json > /tmp/inv_ev.json
bq mk --table --schema /tmp/inv_ev.json \
  --clustering_fields run_id \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.investigation_events"

jq '.schema' terraform/modules/aqa/schemas/trajectories.json > /tmp/traj.json
bq mk --table --schema /tmp/traj.json \
  --clustering_fields agent_name,run_id \
  --time_partitioning_type DAY --time_partitioning_field created_at \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.trajectories"

jq '.schema' terraform/modules/aqa/schemas/trajectory_payloads.json > /tmp/payloads.json
bq mk --table --schema /tmp/payloads.json \
  --clustering_fields agent_name,trajectory_id \
  --time_partitioning_type DAY --time_partitioning_field created_at \
  --time_partitioning_expiration 2592000 \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.trajectory_payloads"

jq '.schema' terraform/modules/aqa/schemas/trajectory_payload_turns.json > /tmp/turns.json
bq mk --table --schema /tmp/turns.json \
  --clustering_fields agent_name,trajectory_id \
  --time_partitioning_type DAY --time_partitioning_field created_at \
  --time_partitioning_expiration 2592000 \
  --label component:aqa --label observed_deployment:"$DEPLOYMENT" \
  "$PROJECT:$DATASET.trajectory_payload_turns"
```

To update tables in an existing deployment when new columns are added (for
example, `analyses`, `analysis_mode`, `occurrence_state`, and `trajectory_ids`
on `insight_occurrences`, or `agent_revision` on `trajectory_payloads`):

```bash
jq '.schema' terraform/modules/aqa/schemas/insight_occurrences.json > /tmp/occ.json
bq update --schema /tmp/occ.json "$PROJECT:$DATASET.insight_occurrences"

jq '.schema' terraform/modules/aqa/schemas/trajectory_payloads.json > /tmp/payloads.json
bq update --schema /tmp/payloads.json "$PROJECT:$DATASET.trajectory_payloads"
```

`bq` reads only table schemas from these files. Clustering, partitioning, and
labels must be provided through CLI flags that match the Terraform
configuration. Schema tests (`tests/test_insights_schema.py`,
`tests/test_investigations_schema.py`, `tests/test_trajectories_schema.py`, and
`tests/test_trajectory_payloads_schema.py`) verify that manual flags stay in
sync with Terraform.

The three trajectory tables require partitioning at creation time, as BigQuery
does not allow adding partitions to existing tables:
- `trajectory_payloads` and `trajectory_payload_turns`: Default to a 30-day
  expiration window (`--time_partitioning_expiration 2592000`), configurable via
  `trajectory_payload_retention_days` in Terraform.
- `trajectories`: Stores metadata without conversation content. It has no
  expiration because it retains the historical data displayed in daily charts.

To change the retention window on an existing deployment, run `bq update
--time_partitioning_expiration <seconds>`. Any partitions older than the updated
threshold are deleted during the next expiration pass.

`BigQueryInsightStore`, `BigQueryInvestigationStore`, and
`BigQueryTrajectoryStore` initialize with `CREATE_NEVER`. Missing tables cause
operations to fail immediately rather than creating tables with inferred
schemas. Schema tests validate every writer and query against the schema
definitions. `BigQueryTrajectoryStore` manages all three trajectory tables,
while `BigQueryInsightStore` manages insights and occurrences.

**Archiving a conversation is a critical write operation.** While other write
errors are logged and skipped (costing only a single dashboard entry), losing an
archived conversation discards the evidence backing an insight. Therefore,
`BigQueryTrajectoryStore.record_payloads` retries and raises on failure. You
must create both payload tables before deploying an engine that writes to them;
otherwise, sweeps fail immediately.

Payload tables append records without deduplication. If two sweeps sample the
same conversation, both records are stored and distinguished by `created_at`.
Readers select a single record using `bigquery_reader.PAYLOAD_PREFERENCE`, while
duplicate rows remain until their partition expires. Consequently, counting rows
in `trajectory_payloads` reflects stored copies rather than unique
conversations.

The `investigations` table is also required; scheduling an investigation fails
immediately if this table is missing. Conversely, writing to
`investigation_events` is best-effort: if missing, the run still completes,
though progress events are not recorded.

### Cost attribution with labels

Cloud Billing does not track caller identities. The standard billing export
categorizes costs by `project + service + SKU (+ resource)` along with custom
**labels**, where "service" refers to the Google Cloud product rather than the
application. An observed agent and AQA deployed on Agent Runtime in the same
project share the same billing service and similar SKUs (such as Agent
Engine vCPU/GB-hour and Gemini tokens). Unless deployed in separate projects,
[labels](https://cloud.google.com/resource-manager/docs/labels-overview) are the
only way to separate their costs.

AQA applies the label **`component=aqa`** across its resources:

| Surface | Where it comes from |
|---|---|
| GCS buckets | `gcloud storage buckets update --update-labels=component=aqa` (as shown above, since `buckets create` does not support labels), or the `terraform/modules/aqa` module |
| BigQuery insight dataset | `labels` on the dataset and both tables in `terraform/modules/aqa/insights.tf` |
| Agent Runtime resource | **Currently unlabeled** — see the known gaps below |
| Chat UI Cloud Run service | `--labels=component=aqa` in `ui/deploy_ui_cloud_run.sh` |
| Every Gemini request | `core/labels.py` → `LocatedGemini._preprocess_request` (for all ADK agents) and direct GenAI call sites (`genai_json.call_gemini` for insight clustering, merging, verification, and same-issue evaluation) |
| Every BigQuery job AQA submits | `core/labels.py` → ingestion queries and insight store operations |

Using consistent labels groups infrastructure and model spending into a single
billing report. To analyze costs by label, filter the [billing
export](https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/standard-usage)
on `labels` or `system_labels`, or group by **Cost breakdown → Label** in the
Cloud Console.

Known labeling gaps:
- **Managed rubric-based metric requests**: Managed rubric-based metrics do
  not support per-request labels. The prebuilt metrics use the default judge
  model, a judged custom metric may name one with `judge_model`, and
  `EvaluateMethodConfig` lacks a `labels` field either way, leaving evaluation
  calls unlabeled.
- **Agent Runtime resource**: The Agent Runtime resource itself currently does not
  carry the `component=aqa` label.

## 2. Deploy

Set runtime environment variables (see [`.env.example`](../.env.example))
and deploy. Agent Runtime automatically sets `GOOGLE_CLOUD_PROJECT`,
`GOOGLE_CLOUD_LOCATION`, `GOOGLE_GENAI_USE_VERTEXAI`, and
`GOOGLE_CLOUD_AGENT_ENGINE_ID`.

```bash
cd ~/ambient-quality-agent
export GOOGLE_CLOUD_PROJECT=your-gcp-project
export GOOGLE_CLOUD_LOCATION=us-central1
export AQA_JOBS_GCS_BUCKET="${GOOGLE_CLOUD_PROJECT}-aqua-jobs"
export AQA_OBSERVED_AGENT_NAME=your_observed_agent
# Set any additional AQA_*, BIGQUERY_*, *_METRICS, or DATA_* variables needed.
```

To evaluate sessions using deterministic Python checks alongside model
evaluations, publish a metric library to `AQA_METRICS_GCS_BUCKET` using
`agents-cli aqua metrics publish`. Published metrics run during session-review
sweeps over the specified window; if no metrics are published, the review runs
using model evaluations alone. Terraform provisions the metrics bucket and
grants read access to the engine; define authorized publishers in the
`metrics_writers` variable. Because published metrics execute inside the agent
process using its credentials, write access to this bucket grants permissions
equivalent to deploying code. See the [agents-cli evaluation
guide](https://google.github.io/agents-cli/guide/evaluation/).

**Production deployments use `agents-cli`**, which builds the repository
[`Dockerfile`](../Dockerfile) on Agent Runtime. The resulting image serves the
runtime contract directly from `ambient_quality_agent.fast_api_app`. Deploying
the repository Dockerfile ensures the running container matches the
security-reviewed source code rather than a template generated at deploy time.

### Local iteration with `adk deploy`

> [!WARNING]
> **`adk deploy` does not use this repository's `Dockerfile`.** It writes its
> own from a template into a temporary staging folder and deploys that.

Two files in the agent package require attention, as `adk deploy` uploads only
that directory:
- **`requirements.txt`**: Used by `pip` when building the generated Dockerfile.
  It is generated from `pyproject.toml`; do not edit it manually (`make lint`
  regenerates it, and `make lint-check` validates consistency).
- **`.ae_ignore`**: Excludes `__pycache__`, `.adk` state, and `.env` from staged
  uploads. Note that this file does not use `.gitignore` syntax; see the
  comments inside the file.

ADK reads deployment environment variables exclusively from
`src/ambient_quality_agent/.env`.

```bash
uv run adk deploy agent_engine \
  --project="$GOOGLE_CLOUD_PROJECT" \
  --region="$GOOGLE_CLOUD_LOCATION" \
  --display_name=aqa-scratch \
  --artifact_service_uri="gs://${AQA_JOBS_GCS_BUCKET}" \
  src/ambient_quality_agent

# To update an existing scratch engine:
#   ... --agent_engine_id=<ENGINE_ID> src/ambient_quality_agent
```

You do **not** need to set `--session_service_uri`: ADK defaults to
`agentengine://<resource_name>`, which satisfies durable investigation
requirements (matching the value computed by
`services._resolve_session_service_uri()`).

## 3. Verify

```bash
# Read engine logs (check for "Submitted durable investigation" and workflow log entries):
gcloud logging read \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine"
   AND resource.labels.reasoning_engine_id="<ENGINE_ID>"' \
  --project=your-gcp-project --limit=40 --freshness=30m \
  --format="value(timestamp,severity,textPayload)"

# Inspect durable job I/O in GCS (full event stream including state_delta):
gcloud storage ls -r "gs://your-gcp-project-aqua-jobs/aqa-jobs/"
```

To verify an investigation interactively in chat:
1. Request an investigation. The agent returns a `run_id` immediately with a
   `pending` status.
2. Check the status using `status of <run_id>`. The state transitions to
   `running`.
3. Check status again once the durable LRO completes; the state reports `done`
   along with an investigation summary.

## Notes & limitations

- **Cold-start latency**: The durable LRO environment cold-starts before
  execution begins. Because of this initial startup, even brief jobs can take
  several minutes to finish. The user-facing turn returns immediately, so poll
  the status instead of blocking on completion.
- **Local runs**: Running locally (`adk run src/ambient_quality_agent`) lacks an
  engine instance to query, causing `schedule_investigation` to fail (missing
  `GOOGLE_CLOUD_AGENT_ENGINE_ID`). Durable execution operates only in deployed
  environments; test the investigation graph locally using unit tests
  (`run_investigation_graph`).
- **Organization policy restrictions on IAM policies**: If an organization
  policy restricts `iam.serviceAccounts.setIamPolicy`, Terraform fails on
  `engine_token_creator` and `engine_acts_as_caller` with a 403 error. Because
  the restriction applies at the organization level, project-level roles cannot
  override it. To resolve this, set `act_as_grant_scope = "project"` to grant
  `roles/iam.serviceAccountTokenCreator` and `roles/iam.serviceAccountUser`
  directly on the project.

## 4. Deploy the dashboard to Cloud Run

The web dashboard is located in [`ui/`](../ui). It is a standalone FastAPI
service containing its own Dockerfile, dependency specifications, and
`agents-cli` manifest. The service queries the deployed engine over its command
and A2A routes, displaying lists of investigations and insights
along with detail views for configurations, individual investigations, or
insights.

**Deploying with `agents-cli`**: The deployment command `agents-cli deploy
--deploy-aqua` deploys the dashboard as its final step onto the Cloud Run service created by
`agents-cli infra single-project`. Pass `--skip-aqua-ui` to omit the dashboard
image. See the module docstring in [`extension/acli_ui.py`](acli_ui.py) for details
on how the build context is assembled from `ui/` and the wire client, and why
the deployment step expects the service to exist beforehand.

**Resource ownership**: Terraform manages the Cloud Run service infrastructure,
while the deploy step updates the container image running on it. The Terraform
module provisions the Cloud Run service, its service account, the
`roles/aiplatform.user` IAM role, and configures `AGENT_ENGINE_RESOURCE_ID` to
reference the engine. Applying the deployment root always provisions this
service. It defaults to Cloud Run `all` ingress; if an organization enforces
`constraints/run.allowedIngress`, an organization policy exception is required
for the project.

### Deploying the image without `agents-cli`

The script below deploys the service using `gcloud` directly, which is useful
when targeting a service managed outside `agents-cli` or when iterating on the
image alone:

```bash
ui/deploy_ui_cloud_run.sh
# Target settings default to the active engine; override using environment variables:
#   PROJECT=my-proj REGION=us-central1 SERVICE=aqa-ui \
#   AGENT_ENGINE_RESOURCE_ID=projects/.../reasoningEngines/... \
#   ALLOW_UNAUTH=true \
#   ui/deploy_ui_cloud_run.sh
# Set CLOUDSDK_CORE_DISABLE_PROMPTS=1 to auto-confirm the one-time Artifact
# Registry repo prompt (e.g. in CI or automated runs).
```

The script creates a dedicated runtime service account, grants it
`roles/aiplatform.user` on the engine project (to call the engine's routes),
and builds and deploys from source using `ui/Dockerfile`. It requires
authentication by default (set `ALLOW_UNAUTH=true` to allow unauthenticated
access). See the UI README for details on UI panels and local development.

### Browser access with Identity-Aware Proxy (the default)

The service deploys with `ALLOW_UNAUTH=false`, blocking unauthenticated
requests. **Identity-Aware Proxy (IAP)** protects the `run.app` URL using Google
SSO, allowing authorized users to open the URL directly in their browser without
running a local proxy or port forward.

`ui_iap_enabled` is enabled by default. Specify authorized users or groups in
the deployment root Terraform configuration; no one gets access until you grant
it:

```hcl
ui_iap_members = ["group:my-team@example.com"]   # Users or groups allowed to access the UI
```

Running `agents-cli infra single-project --apply` configures IAP on the service,
grants `roles/run.invoker` to the IAP service agent, and grants
`roles/iap.httpsResourceAccessor` to the specified members. To grant access to
additional users later, run:

```bash
gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run --service=aqa-ui --region=<region> \
  --member="user:someone@example.com" --role="roles/iap.httpsResourceAccessor"
```

### The proxy, where IAP is not available

Within a Google organization, IAP uses the organization's managed OAuth client,
requiring no OAuth consent screen configuration. Projects without an
organization ancestor lack a managed client, and GCP does not support creating
the required OAuth credentials programmatically; in that case, Google recommends
enabling IAP initially through the Cloud Console. Run `gcloud projects
get-ancestors <project>` to determine whether your project belongs to an
organization.

If IAP is not available, set `ui_iap_enabled = false` and connect through an
authenticated local proxy:

```bash
gcloud run services proxy aqa-ui --region=<region> --project=<project>
# Open the printed URL in your browser (defaults to http://localhost:8080).
```

> Note: in an `agents-cli` deployment, run `agents-cli aqua ui-proxy`.

To allow another user to connect through the proxy, grant them
`roles/run.invoker` on the service:

```bash
gcloud run services add-iam-policy-binding aqa-ui \
  --region=<region> --project=<project> \
  --member="user:someone@example.com" --role="roles/run.invoker"
```

Choose either IAP or the proxy. When IAP is enabled, it intercepts all traffic
directed to the `run.app` URL that the local proxy forwards to.
