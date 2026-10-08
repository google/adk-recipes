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

variable "project_id" {
  description = "Google Cloud project that hosts the AQA deployment."
  type        = string
}

variable "region" {
  description = "Location for every regional resource."
  type        = string
  default     = "us-central1"
}

variable "observed_deployment_name" {
  description = "The observed agent's deployment, as its project is named. Every resource is named from it, as `<deployment>-aqua`."
  type        = string
}

variable "observed_agent_name" {
  description = <<-EOT
    The agent AQA watches, as ADK names it. Its telemetry carries this as
    `gen_ai.agent.name` and AQA filters on it; the engine sees it as
    `AQA_OBSERVED_AGENT_NAME`. Distinct from `observed_deployment_name`, which
    names infrastructure and so cannot hold the underscores an ADK name has.
    Null derives `observed_deployment_name` with hyphens replaced by
    underscores.
  EOT
  type        = string
  default     = null
}

variable "configure_observed_agent" {
  description = <<-EOT
    Whether AQuA is deployed with an observed agent.

    - true: the engine's environment names the observed agent and its telemetry
      settings. AQuA's service account gets read access to the agent's
      telemetry, and the deployment creates the agent's scheduled tick and
      update sink.
    - false: AQuA is deployed with no observed agent. The engine's environment
      has no agent name and no telemetry settings. No telemetry access is
      granted. Attach the agent later with `agents-cli aqua attach`, whose
      `--apply` creates its scheduled tick and update sink from `../attach`,
      and grant the telemetry access by hand. `observed_agent_name`, the
      `telemetry_*` inputs, `agent_revision_trigger_delay_seconds`,
      `observed_engine_id`, `investigation_schedule` and
      `scheduled_trigger_enabled` are ignored.
  EOT
  type        = bool
  default     = true
}

variable "observed_engine_id" {
  description = <<-EOT
    Numeric id of the Agent Runtime agent to watch, as `1234567890` -- the last
    segment of its resource name. Null turns update triggering off: neither the
    audit sink nor its publish grant is created. Null is the setting before
    first deployment, and when the observed agent is not on Agent Runtime.
    A Cloud Run or GKE deployment emits no `UpdateReasoningEngine` audit entry.
  EOT
  type        = string
  default     = null
}

# Kept so a caller that still sets it keeps working.
# TODO: temporary. Delete once no caller sets it, meaning `agents-cli` passes
# `observed_engine_id` and no fetched `terraform.tfvars` names this.
variable "observed_engine_resource_name" {
  description = "Deprecated. Use `observed_engine_id`. Only the last segment is read."
  type        = string
  default     = null
}

variable "investigation_schedule" {
  description = "The schedule for the periodic investigation, as a crontab entry. Midday UTC each day by default. Each run checks everything since the last run that finished."
  type        = string
  default     = "0 12 * * *"
}

variable "task_queue_name" {
  description = "Cloud Tasks queue name. Defaults to `<observed_deployment_name>-aqua-delay`. Use a unique name if rebuilding within 7 days of deletion."
  type        = string
  default     = null
}

variable "agent_revision_trigger_delay_seconds" {
  description = "Wait after an observed-agent revision update before investigating, so the new revision's traces have time to accumulate."
  type        = number
  default     = 900
}

variable "scheduled_trigger_enabled" {
  description = "Whether the periodic investigation runs. On by default, and each run costs one investigation in model spend."
  type        = bool
  default     = true
}

variable "act_as_grant_scope" {
  description = <<-EOT
    Where service-account IAM bindings (`roles/iam.serviceAccountTokenCreator`
    on the engine account and `roles/iam.serviceAccountUser` on the ambient
    caller) are granted: on the target accounts (`service_account`, the
    default), on the project (`project`, for an organisation that denies setting
    IAM policy on a service account), or not at all (`none`). See the module
    variable for what each costs.
  EOT
  type        = string
  default     = "service_account"
}

variable "telemetry_dataset" {
  description = <<-EOT
    Dataset holding the OTel export that `agents-cli` configures for the
    observed agent, which AQA reads to find its work. Not the BigQuery
    Analytics dataset. Null derives `<observed_deployment_name>_telemetry`.
  EOT
  type        = string
  default     = null
}

variable "telemetry_table" {
  description = <<-EOT
    Table name in the telemetry dataset, passed into the module. Null defaults
    to the `agent_runtime` table; override for other targets.
  EOT
  type        = string
  default     = null
}

variable "telemetry_ingestion_source" {
  description = <<-EOT
    Telemetry store AQA reads: `cloud_logging` (default export sink table),
    `cloud_ops` (Cloud Trace linked dataset), or `big_query`. For `cloud_ops`,
    `telemetry_dataset`, `telemetry_table`, and `telemetry_location` must also
    be set; see the module variable for details.
  EOT
  type        = string
  default     = "cloud_logging"
}

variable "telemetry_location" {
  description = <<-EOT
    BigQuery location for telemetry queries. Null defaults to `region`. Set
    when the dataset is in a different location, such as the `US` multi-region
    commonly used by Cloud Trace linked datasets.
  EOT
  type        = string
  default     = null
}

variable "ui_iap_enabled" {
  description = <<-EOT
    Front the UI with Identity-Aware Proxy (Google SSO on its run.app URL)
    instead of `gcloud run services proxy`. On by default; turn it off in a
    project with no organisation ancestor. See the module variable for details.
  EOT
  type        = bool
  default     = true
}

variable "ui_iap_members" {
  description = <<-EOT
    Principals granted access to the UI through IAP (roles/iap.httpsResourceAccessor).
    Empty provisions none, so no one can open the UI until the role is granted.
    Ignored unless ui_iap_enabled is on.
  EOT
  type        = list(string)
  default     = []
}

variable "force_destroy" {
  description = <<-EOT
    Whether a `terraform destroy` may take down the buckets, the insight tables
    and the rest of their dataset.
  EOT
  type        = bool
  default     = true
  nullable    = false
}

variable "notification_emails" {
  description = <<-EOT
    Addresses notified when an investigation fails or finds a new defect.
    Empty creates no channel and no policy, so no notification is sent. An
    address that is a Google Group must be set to accept mail from
    `alerting-noreply@google.com`, or delivery fails silently.
  EOT
  type        = list(string)
  default     = []
}

variable "trace_readers" {
  description = <<-EOT
    Principals permitted to download exported trace spans under `aqa-telemetry/`
    in the jobs bucket. Cloud Storage IAM applies at the bucket level, so this
    also grants read access to task state and chat transcripts.
  EOT
  type        = list(string)
  default     = []
}

variable "trace_retention_days" {
  description = <<-EOT
    Retention period in days for exported trace spans under `aqa-telemetry/` in the
    jobs bucket.
  EOT
  type        = number
  default     = 30
  nullable    = false
}

variable "engine_cpu" {
  description = <<-EOT
    vCPU for the engine, passed into the module. One of 1, 2, 4, 6 or 8, and it
    bounds `engine_memory`.
  EOT
  type        = string
  default     = "4"
  nullable    = false
}

variable "engine_memory" {
  description = "Memory for the engine, passed into the module, from 1Gi to 32Gi."
  type        = string
  default     = "8Gi"
  nullable    = false
}
