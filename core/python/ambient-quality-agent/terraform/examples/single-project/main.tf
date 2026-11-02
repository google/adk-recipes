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

# One AQA deployment in one project. Apply `../../bootstrap` to the project
# first; it enables the APIs everything here calls.
module "aqa" {
  source = "../../modules/aqa"

  project_id                = var.project_id
  region                    = var.region
  observed_deployment_name  = var.observed_deployment_name
  scheduled_trigger_enabled = var.scheduled_trigger_enabled
  act_as_grant_scope        = var.act_as_grant_scope
  ui_iap_enabled            = var.ui_iap_enabled
  ui_iap_members            = var.ui_iap_members
  task_queue_name           = var.task_queue_name

  trace_readers        = var.trace_readers
  trace_retention_days = var.trace_retention_days

  force_destroy       = var.force_destroy
  notification_emails = var.notification_emails
  engine_cpu          = var.engine_cpu
  engine_memory       = var.engine_memory

  storage_backend = var.storage_backend
  cloudsql_tier   = var.cloudsql_tier
}
