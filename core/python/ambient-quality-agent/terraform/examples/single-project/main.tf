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

locals {
  # The deprecated `observed_engine_resource_name` holds a full resource name
  # whose last segment is the engine id. Only its project segment was ever
  # wrong, so `basename()` keeps the id and drops the part that was in doubt.
  # A caller that still sets the old input therefore derives the same value.
  # TODO: remove the `basename()` branch and the deprecated variable it reads,
  # once no caller sets `observed_engine_resource_name`.
  observed_engine_id = (
    var.observed_engine_id != null ? var.observed_engine_id :
    var.observed_engine_resource_name != null ? basename(var.observed_engine_resource_name) :
    null
  )
}

# One AQA deployment in one project. Apply `../../bootstrap` to the project
# first; it enables the APIs everything here calls.
module "aqa" {
  source = "../../modules/aqa"

  project_id                = var.project_id
  region                    = var.region
  observed_deployment_name  = var.observed_deployment_name
  observed_agent_name       = var.observed_agent_name
  configure_observed_agent  = var.configure_observed_agent
  observed_engine_id        = local.observed_engine_id
  investigation_schedule    = var.investigation_schedule
  scheduled_trigger_enabled = var.scheduled_trigger_enabled
  act_as_grant_scope        = var.act_as_grant_scope
  ui_iap_enabled            = var.ui_iap_enabled
  ui_iap_members            = var.ui_iap_members
  task_queue_name           = var.task_queue_name

  agent_revision_trigger_delay_seconds = var.agent_revision_trigger_delay_seconds
  # TODO b/538445174 - check if it can be derived
  telemetry_dataset = var.telemetry_dataset
  telemetry_table   = var.telemetry_table

  telemetry_ingestion_source = var.telemetry_ingestion_source
  telemetry_location         = var.telemetry_location

  trace_readers        = var.trace_readers
  trace_retention_days = var.trace_retention_days

  force_destroy       = var.force_destroy
  notification_emails = var.notification_emails
  engine_cpu          = var.engine_cpu
  engine_memory       = var.engine_memory
}
