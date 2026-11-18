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

output "project_id" {
  description = "GCP Project ID"
  value       = var.project_id
}

output "region" {
  description = "Deployment region"
  value       = var.region
}

output "artifact_registry_repo" {
  description = "Artifact Registry repository URL"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker.repository_id}"
}

output "cloudrun_service_account" {
  description = "Cloud Run service account email"
  value       = google_service_account.cloudrun.email
}

output "build_service_account" {
  description = "Cloud Build service account of make deploy (source builds)"
  value       = google_service_account.build.email
}

output "web_service_account" {
  description = "Account make web mints the proxy's identity token as (Cloud Run Invoker on the service, granted by make deploy)"
  value       = google_service_account.web.email
}
