# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Inputs for scripts/deploy.py, built from variables so `terraform console` works before the first apply.
locals {
  app_url = "https://${var.service_name}-${data.google_project.current.number}.${var.region}.run.app"

  deploy = {
    service_name                   = var.service_name
    service_account_email          = "${local.service_account_id}@${var.project_id}.iam.gserviceaccount.com"
    computer_zone                  = local.computer_zone
    computer_network               = local.computer_network_name
    computer_service_account_email = "${local.computer_service_account_id}@${var.project_id}.iam.gserviceaccount.com"
    computer_snapshot_policy       = "projects/${var.project_id}/regions/${var.region}/resourcePolicies/${var.service_name}-home-snapshots"
    computer_deployment            = var.service_name
    credential_env_keys            = local.credential_env_keys
    credential_secrets             = local.credential_secrets
    managed_secrets                = var.enable_cloud_sql ? { THREAD_DB_URL = local.thread_db_secret_id } : {}
  }
}

output "deploy" {
  value       = local.deploy
  description = "Inputs for scripts/deploy.py."
}

output "service_url" {
  value       = google_cloud_run_v2_service.app.uri
  description = "App URL (behind IAP)."
}

# The deterministic URL (`https://<service>-<number>.<region>.run.app`); deploy.py sets
# it as APP_URL and as the computers' COMPUTER_MCP_UPSTREAM.
output "app_url" {
  value       = local.app_url
  description = "The app's deterministic Cloud Run URL (APP_URL)."
}

output "computer_instance_template" {
  value       = google_compute_instance_template.computer.self_link
  description = "Instance template every computer is created from (COMPUTER_INSTANCE_TEMPLATE)."
}

output "agent_engine" {
  value       = google_vertex_ai_reasoning_engine.memory.id
  description = "The Agent Engine holding Memory Bank, as its full resource name (GOOGLE_CLOUD_AGENT_ENGINE_ID; memory.tf). Known only after an apply."
}

output "computer_template_label" {
  value       = local.computer_template_label
  description = "The `template` label the current template puts on its computers (also in the template's properties.labels)."
}

output "computer_zone" {
  value       = local.computer_zone
  description = "Zone of users' computers and disks (COMPUTER_ZONE)."
}

output "computer_service_account" {
  value       = google_service_account.computer.email
  description = "Service account the computers run as (COMPUTER_SERVICE_ACCOUNT)."
}

output "app_service_account" {
  value       = google_service_account.app.email
  description = "The app's runtime service account; computers accept daemon calls only from it (COMPUTER_ALLOWED_CALLERS)."
}

output "computer_snapshot_policy" {
  value       = google_compute_resource_policy.home_snapshots.self_link
  description = "Snapshot schedule attached to every home disk (COMPUTER_SNAPSHOT_POLICY)."
}

output "computer_image_repository" {
  value       = local.computer_repo_url
  description = "Artifact Registry repository `make computer-image` pushes to."
}

output "computer_image" {
  value       = local.computer_image
  description = "The image the current template runs."
}

# How the daemon's MCP relay authenticates to the app through IAP: a JWT for the
# computer account, signed with the IAM Credentials API, whose `aud` is the app URL
# with a path wildcard. Cloud Run's built-in IAP uses a Google-managed OAuth client,
# which doesn't accept Google-issued ID tokens from service accounts; see
# https://cloud.google.com/iap/docs/authentication-howto#authenticate_with_a_service_account_jwt
output "computer_mcp_audience" {
  value       = "${local.app_url}/*"
  description = "The `aud` of the signed JWT the computers send IAP (COMPUTER_MCP_AUDIENCE)."
}

# scripts/deploy.py sets this as VOICE_AGENT_IAP_AUDIENCE (it computes it with gcloud, so
# it also works before the first apply); shown here for checking a deployment.
output "iap_audience" {
  value       = "/projects/${data.google_project.current.number}/locations/${var.region}/services/${google_cloud_run_v2_service.app.name}"
  description = "The audience of the IAP JWTs the service verifies (VOICE_AGENT_IAP_AUDIENCE)."
}
