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

variable "task_queue_name" {
  description = "Cloud Tasks queue name. Defaults to `<observed_deployment_name>-aqua-delay`. Use a unique name if rebuilding within 7 days of deletion."
  type        = string
  default     = null
}

variable "scheduled_trigger_enabled" {
  description = "Whether the scheduler tick runs. Off pauses every scheduled investigation of this instance; an agent's own schedule is set and paused with `agents-cli aqua attach`."
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
    and the rest of their dataset, and the Cloud SQL instance.
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

variable "storage_backend" {
  description = "Where AQuA keeps its own state, `bigquery` or `cloudsql`, passed into the module. Changing it replaces the engine."
  type        = string
  default     = "bigquery"
  nullable    = false
}

variable "cloudsql_tier" {
  description = "Cloud SQL machine tier, passed into the module. Used only with `storage_backend = \"cloudsql\"`."
  type        = string
  default     = "db-g1-small"
  nullable    = false
}
