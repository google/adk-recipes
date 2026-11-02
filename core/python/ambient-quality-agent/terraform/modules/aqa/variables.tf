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
  description = "Google Cloud project that hosts every resource in this module."
  type        = string
}

variable "observed_deployment_name" {
  description = <<-EOT
    The observed agent's *deployment*, as its project is named. Every resource
    this module creates is named from it, so two deployments in one project
    never collide.

    An infrastructure identity only. The agents AQA watches are attached to the
    deployed engine with `agents-cli aqua attach`.
  EOT
  type        = string

  validation {
    # Also becomes a service account id, which must start with a letter and
    # end with a letter or a digit.
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.observed_deployment_name))
    error_message = "Must be lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "region" {
  description = "Location for every regional resource the module creates."
  type        = string
  default     = "us-central1"

  validation {
    # A multi-region such as `US` is a valid bucket location that Gemini platform
    # rejects, so accepting one here fails much later and somewhere else.
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "Must be a region such as us-central1, not a multi-region."
  }
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

  validation {
    # A channel is keyed by the string it carries, so two spellings of one
    # address are two channels and two mails, and the apply reports success.
    condition = length(distinct([
      for e in var.notification_emails : lower(trimspace(e))
    ])) == length(var.notification_emails)
    error_message = "Each address must appear once, ignoring case and surrounding space."
  }
}

variable "trace_readers" {
  description = <<-EOT
    IAM members permitted to download exported trace spans under `aqa-telemetry/`
    in the jobs bucket without write access to investigation state.

    Cloud Storage IAM does not support prefix filtering, so granted principals
    can read all objects in the jobs bucket, including task state and chat transcripts
    under `tasks/` and chat names under `chat-titles/`.
  EOT
  type        = list(string)
  default     = []

  validation {
    # Prevents duplicate entries from creating redundant IAM bindings.
    condition     = length(distinct(var.trace_readers)) == length(var.trace_readers)
    error_message = "Each principal must appear once."
  }

  validation {
    condition = alltrue([
      for m in var.trace_readers :
      can(regex("^(user|group|serviceAccount|domain|principal|principalSet):", m))
    ])
    error_message = "Each entry must be an IAM member string, e.g. 'group:quality@example.com'."
  }
}

variable "trace_retention_days" {
  description = <<-EOT
    Retention period in days for exported trace spans under `aqa-telemetry/` in the
    jobs bucket. Spans contain conversation turns; bounding retention limits data
    exposure while keeping recent traces available for dumps.
  EOT
  type        = number
  default     = 30
  nullable    = false

  validation {
    # Requires at least one day so Cloud Storage lifecycle sweeps do not delete
    # new spans before operators can collect them.
    condition = (
      var.trace_retention_days >= 1
      && floor(var.trace_retention_days) == var.trace_retention_days
    )
    error_message = "trace_retention_days must be a positive whole number of days."
  }
}

variable "trajectory_payload_retention_days" {
  description = <<-EOT
    How long an archived conversation is kept, as a partition expiration on
    `trajectory_payloads` and `trajectory_payload_turns`. These hold customer
    conversation content, which is what a retention policy is normally about;
    the `trajectories` index beside them carries no content and is kept
    indefinitely, which is why it takes no setting.

    Long enough that an insight still has its evidence, short enough that the
    copy is not a second permanent archive of the observed agent's traffic.
    Raising it applies to partitions that have not expired yet; lowering it
    deletes on the next expiration sweep, so an insight older than the new
    window loses its conversations.

    When null, defaults to the retention duration configured in
    `schemas/trajectory_payloads.json` and documented in `extension/README.md`.
  EOT
  type        = number
  default     = null

  validation {
    # A ternary rather than `null || ...`, because `||` evaluates both operands
    # on Terraform before 1.12 and this module supports 1.4: the comparison and
    # `floor` would then run on the null default and abort the plan. Only the
    # branch the condition selects is evaluated, on every supported version.
    condition = var.trajectory_payload_retention_days == null ? true : (
      var.trajectory_payload_retention_days >= 1
      && floor(var.trajectory_payload_retention_days) == var.trajectory_payload_retention_days
    )
    error_message = "trajectory_payload_retention_days must be a positive whole number of days."
  }
}

variable "source_snapshot_retention_days" {
  description = <<-EOT
    Retention period in days for observed agent source snapshots in the source bucket.

    Base this on the observed agent's deployment cadence rather than insight
    retention. A duration shorter than the interval between deployments deletes the
    running revision's snapshot.
  EOT
  type        = number
  default     = 30
  nullable    = false

  validation {
    condition = (
      var.source_snapshot_retention_days >= 1
      && floor(var.source_snapshot_retention_days) == var.source_snapshot_retention_days
    )
    error_message = "source_snapshot_retention_days must be a positive whole number of days."
  }
}

variable "task_queue_name" {
  description = <<-EOT
    Cloud Tasks queue name. Defaults to `<observed_deployment_name>-aqua-delay`.
    Use a unique name if rebuilding within 7 days of deletion.
  EOT
  type        = string
  default     = null
}

variable "scheduled_trigger_enabled" {
  description = <<-EOT
    Whether the scheduler tick runs. Off pauses every scheduled investigation
    of this instance, whatever the attached agent's schedule says; an agent's
    own schedule is paused with `agents-cli aqua attach --no-scheduled-trigger`.
    Redeploying the watched agent triggers an investigation even when this is
    off.
  EOT
  type        = bool
  default     = true
}

variable "act_as_grant_scope" {
  description = <<-EOT
    Where service-account IAM bindings (`roles/iam.serviceAccountTokenCreator`
    for the Agent Runtime service agent on the engine account, and
    `roles/iam.serviceAccountUser` for the engine on the ambient caller) are
    granted.

      service_account  on the target service accounts alone. The default, and
                       the least privilege that works.
      project          on the project, so the grants apply across every service
                       account in it. Wider, and the way out of an organisation
                       that denies `iam.serviceAccounts.setIamPolicy`: the apply
                       then fails on account-scoped bindings with a 403 that no
                       project role can lift.
      none             not granted. Use only when the bindings are managed
                       outside Terraform; without `serviceAccountTokenCreator`
                       the engine cannot start, and without `serviceAccountUser`
                       an observed-agent update enqueues nothing.
  EOT
  type        = string
  default     = "service_account"
  validation {
    condition     = contains(["service_account", "project", "none"], var.act_as_grant_scope)
    error_message = "act_as_grant_scope must be one of: service_account, project, none."
  }
}

variable "ui_iap_enabled" {
  description = <<-EOT
    Whether to front the UI with Identity-Aware Proxy, so it is reached at its
    run.app URL behind Google SSO instead of a `gcloud run services proxy`
    tunnel. On inside a Google organisation, where IAP uses the Google-managed
    OAuth client and needs nothing beyond this flag.

    Turn it off in a project with no organisation ancestor. There is no managed
    client to use there, and the OAuth client IAP needs instead cannot be
    created programmatically -- Google's guidance is to enable IAP for the first
    time from the console. `gcloud projects get-ancestors <project>` says which
    case a project is in.

    The service is private either way, and the two access paths are
    alternatives: IAP intercepts everything bound for the run.app URL, which is
    what the tunnel connects to.
  EOT
  type        = bool
  default     = true
}

variable "ui_iap_members" {
  description = <<-EOT
    Principals allowed to open the UI through IAP, e.g.
    ["user:a@example.com", "group:team@example.com"]. Granted
    roles/iap.httpsResourceAccessor. Empty provisions no access, so the UI
    turns everyone away until someone is granted the role -- here, or later
    with `gcloud iap web add-iam-policy-binding`. Ignored unless
    ui_iap_enabled is on.

    Any IAM principal identifier works, `domain:example.com` included, which
    admits every identity in that domain at once. The Google-managed OAuth
    client only lets in identities from the project's own organisation, so a
    domain grant cannot reach past it.
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

variable "engine_cpu" {
  description = <<-EOT
    vCPU for the engine. Agent Runtime accepts 1, 2, 4, 6 or 8, and the value
    bounds `engine_memory`: 1 allows up to 4Gi, 2 up to 8Gi, 4 up to 16Gi, 6 up
    to 24Gi and 8 up to 32Gi.
  EOT
  type        = string
  default     = "4"
  nullable    = false

  validation {
    condition     = contains(["1", "2", "4", "6", "8"], var.engine_cpu)
    error_message = "Must be one of 1, 2, 4, 6 or 8."
  }
}

variable "engine_memory" {
  description = <<-EOT
    Memory for the engine, bounded by `engine_cpu`. Each serving worker holds
    its own copy of the imported agent, so the resting floor is a multiple of
    that rather than of the data one investigation reads.
  EOT
  type        = string
  default     = "8Gi"
  nullable    = false

  validation {
    condition     = can(regex("^([1-9]|[12][0-9]|3[0-2])Gi$", var.engine_memory))
    error_message = "Must be a whole number of Gi from 1Gi to 32Gi."
  }
}

variable "storage_backend" {
  description = <<-EOT
    Where AQuA keeps its own state:

      bigquery  The insights dataset. The default.
      cloudsql  A Cloud SQL for PostgreSQL instance with a private IP, reached
                through a PSC interface on the engine. Adds a VPC, private
                services access, a network attachment, the instance and an IAM
                database user.

    Changing it replaces the engine. The dataset is created either way.
  EOT
  type        = string
  default     = "bigquery"
  nullable    = false

  validation {
    condition     = contains(["bigquery", "cloudsql"], var.storage_backend)
    error_message = "Must be bigquery or cloudsql."
  }
}

variable "network" {
  description = <<-EOT
    An existing VPC, as `projects/P/global/networks/N`, for the Cloud SQL path.
    Null creates one. In an existing network the module adds a subnet, a
    network attachment and a private services access range, and fails if the
    network already has a private services access connection. Shared VPC is not
    supported. Used only with `storage_backend = "cloudsql"`.
  EOT
  type        = string
  default     = null

  validation {
    # `can()` takes the whole right operand, so Terraform before 1.12, which
    # evaluates both sides of `||`, never sees an error from a null.
    condition     = var.network == null || can(regex("^projects/[^/]+/global/networks/[a-z]([-a-z0-9]*[a-z0-9])?$", var.network))
    error_message = "Must be projects/P/global/networks/N."
  }
}

variable "network_attachment_subnet_range" {
  description = <<-EOT
    The subnet that holds the engine's PSC interface: an RFC 1918 range of at
    least /28 that is free in the network. Used only with
    `storage_backend = "cloudsql"`.
  EOT
  type        = string
  default     = "10.8.0.0/28"
  nullable    = false

  validation {
    # A /28 is 16 addresses, so host 15 exists only in a range that large.
    condition     = can(cidrhost(var.network_attachment_subnet_range, 15))
    error_message = "Must be a CIDR range of /28 or larger."
  }
}

variable "cloudsql_tier" {
  description = <<-EOT
    Cloud SQL machine tier. The tier sets the connection limit (25 on
    db-f1-micro, 50 on db-g1-small), which the engine's maximum instance count
    times its pool size must stay under. Shared-core tiers are not covered by
    the Cloud SQL SLA. Used only with `storage_backend = "cloudsql"`.
  EOT
  type        = string
  default     = "db-g1-small"
  nullable    = false
}

variable "cloudsql_availability" {
  description = <<-EOT
    ZONAL, or REGIONAL for a standby in a second zone at twice the price. Used
    only with `storage_backend = "cloudsql"`.
  EOT
  type        = string
  default     = "ZONAL"
  nullable    = false

  validation {
    condition     = contains(["ZONAL", "REGIONAL"], var.cloudsql_availability)
    error_message = "Must be ZONAL or REGIONAL."
  }
}
