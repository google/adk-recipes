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

# The AQuA instance the triggers call, as its engine reports it in the
# `trigger_target` of the attach route, under the same names. The project is the
# topic's.

variable "region" {
  description = "AQuA's region. The scheduler job runs there and calls the engine's regional endpoint."
  type        = string
}

variable "aqua_engine" {
  description = "Resource name of AQuA's engine, as `projects/P/locations/L/reasoningEngines/ID`."
  type        = string
}

variable "ambient_topic" {
  description = <<-EOT
    Id of the Pub/Sub topic whose push subscription delivers audit entries to
    the engine, as `projects/<project id>/topics/<name>`. Names this root's
    project, and prefixes its resources' names.
  EOT
  type        = string

  validation {
    condition     = can(regex("^projects/[^/]+/topics/[^/]+$", var.ambient_topic))
    error_message = "Must be a topic id, as projects/<project id>/topics/<name>."
  }
}

variable "ambient_caller_email" {
  description = "Service account Cloud Scheduler calls the engine as."
  type        = string
}

# The agent, as `agents-cli aqua attach` stored it.

variable "observed_agent_name" {
  description = <<-EOT
    The attached agent, as ADK names it. Names this root's resources, so that
    each attached agent has its own.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z_][A-Za-z0-9_]*$", var.observed_agent_name))
    error_message = "Must be a valid Python identifier: ADK rejects any other agent name."
  }
}

variable "observed_engine_id" {
  description = <<-EOT
    Numeric id of the Agent Runtime agent whose updates trigger an
    investigation -- the last segment of its resource name. Null creates no
    audit sink: the observed agent is not on Agent Runtime, or not deployed
    yet. A Cloud Run or GKE deployment emits no `UpdateReasoningEngine` audit
    entry.
  EOT
  type        = string
  default     = null

  validation {
    # A ternary, because `||` evaluates both operands before Terraform 1.12.
    condition     = var.observed_engine_id == null ? true : can(regex("^[0-9]+$", var.observed_engine_id))
    error_message = "Must be the numeric engine id, not its resource name."
  }
}

variable "observed_project_id" {
  description = <<-EOT
    Project of the Agent Runtime agent `observed_engine_id` names, which holds
    its audit log sink. Null means the topic's project, AQuA's own. Creating a
    sink there needs `roles/logging.configWriter` in that project.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.observed_project_id == null ? true : can(regex("^((?:[a-z][a-z0-9.-]*:)?[a-z][a-z0-9-]*|[0-9]+)$", var.observed_project_id))
    error_message = "Must be a project id or number."
  }
}

variable "investigation_schedule" {
  description = <<-EOT
    The schedule for the periodic investigation, as a crontab entry. Midday
    UTC each day by default; Cloud Scheduler reads it as UTC.

    Each run checks everything since the last run that finished, so the
    schedule can be irregular. One run never looks back further than the
    agent's lookback window, and a warning says how much older telemetry it
    gave up.
  EOT
  type        = string
  default     = "0 12 * * *"
  nullable    = false
}

variable "scheduled_trigger_enabled" {
  description = <<-EOT
    Whether the periodic investigation runs. Each run costs one investigation
    in model spend. Updates to the observed agent trigger an investigation
    either way, when `observed_engine_id` is set.
  EOT
  type        = bool
  default     = true
  nullable    = false
}
