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

# State is local on purpose; README.md#state shows how to add a GCS backend.
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source = "hashicorp/google"
      # 7.21+: `iap_enabled` on google_cloud_run_v2_service (IAP directly on Cloud Run).
      version = ">= 7.21, < 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  apps = yamldecode(file("${path.module}/../config/integrations.yaml")).apps

  # The Google OAuth client must belong to this project, where these APIs are enabled.
  google_workspace_apis = local.apps.google_workspace.apis

  required_services = concat(
    [
      "aiplatform.googleapis.com",
      "run.googleapis.com",
      "compute.googleapis.com",
      "oslogin.googleapis.com",
      "storage.googleapis.com",
      "iap.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "secretmanager.googleapis.com",
      "artifactregistry.googleapis.com",
      "cloudbuild.googleapis.com",
      "cloudresourcemanager.googleapis.com",
    ],
    var.enable_cloud_sql ? ["sqladmin.googleapis.com"] : [],
    var.enable_workspace_apis ? local.google_workspace_apis : [],
  )
}

resource "google_project_service" "enabled" {
  for_each = toset(local.required_services)

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

data "google_project" "current" {
  project_id = var.project_id

  depends_on = [google_project_service.enabled]
}
