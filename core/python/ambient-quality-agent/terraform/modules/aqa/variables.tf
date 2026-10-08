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

    An infrastructure identity only. What AQA watches is an agent, and that has
    its own name -- see `observed_agent_name`.
  EOT
  type        = string

  validation {
    # Also becomes a service account id, which must start with a letter and
    # end with a letter or a digit.
    condition     = can(regex("^[a-z][a-z0-9-]*[a-z0-9]$", var.observed_deployment_name))
    error_message = "Must be lowercase letters, digits and hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "observed_agent_name" {
  description = <<-EOT
    The agent AQA watches, named as ADK names it -- what the observed agent
    passes to `Agent(name=...)`. Its telemetry carries that value as the
    `gen_ai.agent.name` attribute, and AQA filters the telemetry dataset on it.
    Reaches the engine as `AQA_OBSERVED_AGENT_NAME`, which is what the code
    calls it throughout.

    Separate from `observed_deployment_name` because the two cannot be one
    string: an ADK agent name is a Python identifier and so usually has
    underscores, while the deployment name becomes a service account id and a
    bucket prefix, neither of which admits them.

    Getting this wrong is silent. Every query matches nothing, so investigations
    run and report on an empty window rather than failing.

    Null derives `observed_deployment_name` with hyphens replaced by
    underscores, which is what `agents-cli` scaffolds for a project of that
    name.
  EOT
  type        = string
  default     = null

  validation {
    condition = (
      var.observed_agent_name == null
      || can(regex("^[A-Za-z_][A-Za-z0-9_]*$", var.observed_agent_name))
    )
    error_message = "Must be a valid Python identifier: ADK rejects any other agent name."
  }
}

variable "observed_engine_id" {
  description = <<-EOT
    Numeric id of the Agent Runtime agent to watch, as `1234567890` -- the last
    segment of its resource name. Null turns update triggering off: neither the
    audit sink nor its publish grant is created. Null is the setting before
    first deployment, and when the observed agent is not on Agent Runtime.
    A Cloud Run or GKE deployment emits no `UpdateReasoningEngine` audit entry.

    Ignored when `configure_observed_agent` is false; the attach root takes it then.
  EOT
  type        = string
  default     = null
}

variable "configure_observed_agent" {
  description = <<-EOT
    Whether AQuA is deployed with an observed agent.

    - true: the engine's environment names the observed agent and its telemetry
      settings. The engine's service account gets read access to the agent's
      telemetry dataset and payload bucket, and, for `cloud_ops`, to the
      project's log views. The module creates the agent's scheduled tick and
      update sink.
    - false: AQuA is deployed with no observed agent. The engine's environment
      has no agent name and no telemetry settings. No telemetry access is
      granted, and no scheduled tick or update sink is created. Attach the agent
      later with `agents-cli aqua attach`, which stores its settings where the
      engine reads them; its `--apply` creates the triggers from
      `../../examples/attach`. Grant the telemetry access by hand.
      `observed_agent_name`, the `telemetry_*` inputs,
      `agent_revision_trigger_delay_seconds`, `observed_engine_id`,
      `investigation_schedule` and `scheduled_trigger_enabled` are ignored.

    `observed_deployment_name` names every resource in both cases.
  EOT
  type        = bool
  default     = true
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

variable "metrics_writers" {
  description = <<-EOT
    IAM members permitted to publish code metrics (e.g. `group:quality@example.com`).
    Published code runs in the agent process; write access equals deploy access.
  EOT
  type        = list(string)
  default     = []

  validation {
    # IAM bindings are keyed by member string, so duplicates would apply
    # redundant bindings that report success anyway.
    condition     = length(distinct(var.metrics_writers)) == length(var.metrics_writers)
    error_message = "Each principal must appear once."
  }

  validation {
    condition = alltrue([
      for m in var.metrics_writers :
      can(regex("^(user|group|serviceAccount|domain|principal|principalSet):", m))
    ])
    error_message = "Each entry must be an IAM member string, e.g. 'group:quality@example.com'."
  }
}

variable "trace_readers" {
  description = <<-EOT
    IAM members permitted to download exported trace spans under `aqa-telemetry/`
    in the jobs bucket without write access to investigation state.

    Cloud Storage IAM does not support prefix filtering, so granted principals
    can read all objects in the jobs bucket, including task state and chat transcripts
    under `tasks/`.
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

variable "telemetry_payload_bucket" {
  description = <<-EOT
    Bucket the observed agent offloads large telemetry payloads to. Rows in the
    telemetry dataset carry only `gs://` pointers into it, so without read access
    every ingested turn arrives empty and an investigation reports nothing while
    raising no error. Null derives `<project_id>-<observed_deployment_name>-logs`.
  EOT
  type        = string
  default     = null
}

variable "telemetry_dataset" {
  description = <<-EOT
    Dataset the observed agent exports its telemetry to, which AQA reads to
    find its work. Null derives `<observed_deployment_name>_telemetry`, the dataset
    an `agents-cli` agent's `telemetry.tf` creates.
  EOT
  type        = string
  default     = null
}

variable "telemetry_table" {
  description = <<-EOT
    Table in `telemetry_dataset` that AQA reads, created and named by the
    observed agent's own `telemetry.tf` after its deployment target. Null
    defaults to the `agent_runtime` table; `bq ls <dataset>` shows what it
    made.
  EOT
  type        = string
  default     = null
}

variable "telemetry_ingestion_source" {
  description = <<-EOT
    Telemetry store AQA reads:

      cloud_logging  Cloud Logging GenAI export sink table (default).
      cloud_ops      Cloud Trace linked BigQuery dataset (not created by this
                     module). Point `telemetry_dataset` at it, set
                     `telemetry_table` to its span view (`_AllSpans`), and set
                     `telemetry_location` to its location. Because the module
                     always passes an explicit table name, leaving
                     `telemetry_table` null queries the default `cloud_logging`
                     table, which does not exist in a linked dataset.
      big_query      ADK BigQuery analytics dataset.
  EOT
  type        = string
  default     = "cloud_logging"

  validation {
    condition     = contains(["big_query", "cloud_ops", "cloud_logging"], var.telemetry_ingestion_source)
    error_message = "telemetry_ingestion_source must be one of: big_query, cloud_ops, cloud_logging."
  }
}

variable "telemetry_location" {
  description = <<-EOT
    BigQuery location where AQA submits telemetry queries. Null defaults to
    `var.region`.

    Set this when the telemetry dataset is in a different location from the
    engine: a Cloud Trace linked dataset is commonly in a multi-region such as
    `US` while the engine runs in a single region such as `us-east1`. A query
    job submitted against the wrong location fails.
  EOT
  type        = string
  default     = null
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

variable "investigation_schedule" {
  description = <<-EOT
    The schedule for the periodic investigation, as a crontab entry. Midday
    UTC each day by default; Cloud Scheduler reads it as UTC unless the job says
    otherwise.

    Each run checks everything since the last run that finished, so the schedule
    can be irregular. There is nothing else to set. One run never looks back
    further than `DATA_LOOKBACK_WINDOW` days, and a warning says how much older
    telemetry it gave up.

    Ignored when `configure_observed_agent` is false; the attach root takes it then.
  EOT
  type        = string
  default     = "0 12 * * *"
}

variable "task_queue_name" {
  description = <<-EOT
    Cloud Tasks queue name. Defaults to `<observed_deployment_name>-aqua-delay`.
    Use a unique name if rebuilding within 7 days of deletion.
  EOT
  type        = string
  default     = null
}

variable "agent_revision_trigger_delay_seconds" {
  description = <<-EOT
    Wait after an observed-agent revision update before investigating, so the
    new revision's traces have time to accumulate. Sets
    `AQA_AGENT_REVISION_TRIGGER_DELAY_SECONDS`. Runtime config overrides still
    take precedence over this default.
  EOT
  type        = number
  default     = 900
  validation {
    condition = (
      var.agent_revision_trigger_delay_seconds >= 0 &&
      floor(var.agent_revision_trigger_delay_seconds) == var.agent_revision_trigger_delay_seconds
    )
    error_message = "agent_revision_trigger_delay_seconds must be a positive integer."
  }
}

variable "scheduled_trigger_enabled" {
  description = <<-EOT
    Whether the periodic investigation runs. On by default, and each run costs
    one investigation in model spend. Redeploying the watched agent triggers an
    investigation even when this is off.

    Ignored when `configure_observed_agent` is false; the attach root takes it then.
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
    and the rest of their dataset.
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
