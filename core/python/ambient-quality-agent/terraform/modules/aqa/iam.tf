# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Without this the engine runs as the project's shared reasoning-engine service
# agent, and every grant below would reach every other engine in the project.
resource "google_service_account" "engine" {
  project      = var.project_id
  account_id   = local.prefix
  display_name = "Ambient Quality Agent watching ${var.observed_deployment_name}"
}

locals {
  engine_member = "serviceAccount:${google_service_account.engine.email}"
}

# For the project number the service agents' emails carry.
data "google_project" "this" {
  project_id = var.project_id
}

# The Agent Runtime service agent mints the token the engine runs as, and its
# own role carries no `iam.serviceAccounts.*`, so the binding has to sit here.
resource "google_service_account_iam_member" "engine_token_creator" {
  count = var.act_as_grant_scope == "service_account" ? 1 : 0

  service_account_id = google_service_account.engine.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# The same grant one level up, for an organisation that denies setting an IAM
# policy on a service account (`iam.serviceAccounts.setIamPolicy`).
resource "google_project_iam_member" "engine_token_creator_project" {
  count = var.act_as_grant_scope == "project" ? 1 : 0

  project = var.project_id
  role    = "roles/iam.serviceAccountTokenCreator"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# A fresh grant takes minutes to reach the engine's start path: seen failing at
# 90 seconds and working at just over three.
resource "time_sleep" "engine_token_creator" {
  depends_on = [
    google_service_account_iam_member.engine_token_creator,
    google_project_iam_member.engine_token_creator_project,
  ]
  create_duration = var.act_as_grant_scope == "none" ? "0s" : "240s"

  triggers = {
    binding = try(
      google_service_account_iam_member.engine_token_creator[0].id,
      google_project_iam_member.engine_token_creator_project[0].id,
      "none",
    )
  }
}

# Pub/Sub dead-letters as its own service agent, and without both of these it
# drops the message silently.

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.ambient_dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.ambient_push.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Delay creation until the service account propagates, and again when it is
# replaced. Everything that names the caller in an API call waits on this: an
# account that exists but has not propagated is reported as denied, not as
# missing, so the apply fails with a permissions error that reads like a real
# one.
resource "time_sleep" "ambient_caller" {
  triggers = {
    service_account = google_service_account.ambient_caller.email
  }

  create_duration = "20s"
}

# Identity used by Cloud Scheduler, Pub/Sub push, and Cloud Tasks.
resource "google_service_account" "ambient_caller" {
  project      = var.project_id
  account_id   = "${local.prefix}-caller"
  display_name = "Ambient trigger caller for ${local.prefix}"

  lifecycle {
    precondition {
      # The longest id the module derives. Checked here because a `validation`
      # block cannot read a local.
      condition     = length("${local.prefix}-caller") <= 30
      error_message = "Derived name '${local.prefix}-caller' exceeds the 30-character limit. Use an `observed_deployment_name` of 2 to 18."
    }
  }
}

# `objectAdmin` rather than a creator role: a retried query job rewrites
# `aqa-jobs/{run_id}.json`, which needs delete.
resource "google_storage_bucket_iam_member" "jobs_writer" {
  bucket = google_storage_bucket.jobs.name
  role   = "roles/storage.objectAdmin"
  member = local.engine_member
}

# Read-only: the agent executes this code and must not rewrite its own checks.
#
# Needs two roles: `objectViewer` reads objects, but `list_blobs` also performs
# a bucket lookup (`storage.buckets.get`) that `objectViewer` lacks.
resource "google_storage_bucket_iam_member" "metrics_reader" {
  bucket = google_storage_bucket.metrics.name
  role   = "roles/storage.objectViewer"
  member = local.engine_member
}

# Provides `storage.buckets.get` for `list_blobs`. Cannot read object bodies,
# so both reader roles are required.
resource "google_storage_bucket_iam_member" "metrics_bucket_reader" {
  bucket = google_storage_bucket.metrics.name
  role   = "roles/storage.legacyBucketReader"
  member = local.engine_member
}

# Requires `objectAdmin` because `metrics publish` mirrors the directory and
# must delete removed metrics.
resource "google_storage_bucket_iam_member" "metrics_writers" {
  for_each = toset(var.metrics_writers)

  bucket = google_storage_bucket.metrics.name
  role   = "roles/storage.objectAdmin"
  member = each.value
}

# Grants read-only access for trace dumps from `aqa-telemetry/` without granting
# write access to investigation task state. Bucket-level IAM cannot be scoped
# to a prefix, so this also grants read access to task state and chat transcripts.
#
# Only `objectViewer` is required because `dump-traces` queries the GCS JSON API
# directly (`storage.objects.list` and `storage.objects.get`) without calling
# `storage.buckets.get`.
resource "google_storage_bucket_iam_member" "trace_readers" {
  for_each = toset(var.trace_readers)

  bucket = google_storage_bucket.jobs.name
  role   = "roles/storage.objectViewer"
  member = each.value
}

# The telemetry rows hold `gs://` pointers, not content. With no observed agent
# configured there is no bucket to name, and the grant is made by hand once one
# is attached.
resource "google_storage_bucket_iam_member" "telemetry_payload_reader" {
  count = var.configure_observed_agent ? 1 : 0

  bucket = local.telemetry_payload_bucket
  role   = "roles/storage.objectViewer"
  member = local.engine_member
}

# The SDK calls `bucket.exists()` before it submits a query job, which needs
# `storage.buckets.get`. No object-level role carries it.
resource "google_storage_bucket_iam_member" "jobs_reader" {
  bucket = google_storage_bucket.jobs.name
  role   = "roles/storage.legacyBucketReader"
  member = local.engine_member
}

# Reading the snapshotted source of the observed agents, and writing it: the
# engine's `source/*` routes write the snapshots the CLI uploads. `objectAdmin`
# rather than a creator role, because publishing a revision again rewrites its
# objects and removes the files it no longer has.
resource "google_storage_bucket_iam_member" "source_writer" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectAdmin"
  member = local.engine_member
}

# Enumerating revisions reaches the bucket resource itself, not just objects:
# the reader resolves the bucket before it runs a delimiter listing, which needs
# `storage.buckets.get`.
resource "google_storage_bucket_iam_member" "source_bucket_reader" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.legacyBucketReader"
  member = local.engine_member
}

# Grants Gemini platform access to invoke publisher models. Publisher models do not
# take resource-level IAM policies. Also covers BigQuery AI.IF model calls from
# conversation selectors, which execute under the engine's credentials.
resource "google_project_iam_member" "aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = local.engine_member
}

# Running the query jobs. Reading what they query is granted per dataset.
resource "google_project_iam_member" "bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.engine_member
}

# Client libraries attach a quota project, and using a project's services is project-level.
resource "google_project_iam_member" "service_usage_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.engine_member
}

# The observed agent's telemetry, which AQA reads to find its work. The dataset
# belongs to that agent's own root, so this adds one access entry instead of
# rewriting the list and dropping their authorized views.
resource "google_bigquery_dataset_access" "telemetry_reader" {
  count = var.configure_observed_agent ? 1 : 0

  project       = var.project_id
  dataset_id    = local.telemetry_dataset
  role          = "roles/bigquery.dataViewer"
  user_by_email = google_service_account.engine.email
}

# A `cloud_ops` linked dataset is backed by Cloud Logging log views, which
# dataset-level access does not cover: queries fail with `403 Permission
# denied for all log views` without `roles/logging.viewAccessor`. This grant is
# project-wide because the module does not name the underlying log view,
# allowing the engine to read any log view in the project. Only created for
# `cloud_ops` deployments.
resource "google_project_iam_member" "engine_log_view_reader" {
  count = var.configure_observed_agent && var.telemetry_ingestion_source == "cloud_ops" ? 1 : 0

  project = var.project_id
  role    = "roles/logging.viewAccessor"
  member  = local.engine_member
}

resource "google_cloud_tasks_queue_iam_member" "engine_enqueuer" {
  project  = var.project_id
  location = google_cloud_tasks_queue.delay.location
  name     = google_cloud_tasks_queue.delay.name
  role     = "roles/cloudtasks.enqueuer"
  member   = local.engine_member
}

# Missing either binding fails the enqueue inside an HTTP 200, so the update
# path stops producing investigations silently.
resource "google_service_account_iam_member" "engine_acts_as_caller" {
  count = var.act_as_grant_scope == "service_account" ? 1 : 0

  depends_on = [time_sleep.ambient_caller]

  service_account_id = google_service_account.ambient_caller.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.engine_member
}

# The same grant one level up, for an organisation that denies setting an IAM
# policy on a service account. Reaches every account in the project, the caller
# included, so it is opt-in rather than a fallback the module picks on its own.
resource "google_project_iam_member" "engine_acts_as_project" {
  count = var.act_as_grant_scope == "project" ? 1 : 0

  project = var.project_id
  role    = "roles/iam.serviceAccountUser"
  member  = local.engine_member
}

resource "google_project_iam_member" "ambient_caller_aiplatform_user" {
  depends_on = [time_sleep.ambient_caller]

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.ambient_caller.email}"
}

# Reading the identity off the sink is what orders this correctly, and what ties
# the grant to the sink's own presence: no sink, no identity to grant.
resource "google_pubsub_topic_iam_member" "sink_publisher" {
  count = local.observe_updates ? 1 : 0

  project = var.project_id
  topic   = google_pubsub_topic.ambient.name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.ambient_updates[0].writer_identity
}

# The addresses the two telemetry grants have in a deployment applied without
# their `count`, so that such a deployment keeps its grants instead of having
# them destroyed and recreated.
moved {
  from = google_storage_bucket_iam_member.telemetry_payload_reader
  to   = google_storage_bucket_iam_member.telemetry_payload_reader[0]
}

moved {
  from = google_bigquery_dataset_access.telemetry_reader
  to   = google_bigquery_dataset_access.telemetry_reader[0]
}
