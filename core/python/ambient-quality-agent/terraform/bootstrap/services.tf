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
  # Terraform needs these two before it can manage any API; see providers.tf.
  bootstrap_services = [
    "serviceusage.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]

  # What AQA itself calls, plus IAM for the service account each deployment
  # creates.
  services = [
    "iam.googleapis.com",
    "storage.googleapis.com",
    "aiplatform.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "pubsub.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudscheduler.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",

    # The UI Cloud Run service, and the source-deploy that builds its image with
    # Cloud Build and stores it in Artifact Registry.
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
  ]
}

# `disable_on_destroy = false` throughout: these are shared with every other
# workload in the project, so destroying anything must not switch an API off for
# something else.
resource "google_project_service" "bootstrap" {
  provider = google.api_bootstrap
  for_each = toset(local.bootstrap_services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service" "services" {
  for_each = toset(local.services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false

  depends_on = [google_project_service.bootstrap]
}

# Enabling an API is eventually consistent, and the deployment root is separate state.
resource "time_sleep" "propagation" {
  create_duration = "60s"

  # Keyed on the resources so a second project waits again, and to order this after them.
  triggers = {
    services = join(",", [for s in google_project_service.services : s.id])
  }
}
