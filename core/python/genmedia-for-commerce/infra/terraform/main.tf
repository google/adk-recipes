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

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# The Cloud Run service is created and sized by `make deploy`
# (gcloud run deploy --source), not Terraform. Drop it from existing state
# without destroying the running service.
removed {
  from = google_cloud_run_v2_service.main

  lifecycle {
    destroy = false
  }
}

# Enable required APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "cloudbuild.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "iap.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
    "vision.googleapis.com",
    "iam.googleapis.com",
    # V4 signed URLs for the Studio previews (IAM signBlob)
    "iamcredentials.googleapis.com",
    # Gemini Enterprise (make register-gemini-enterprise)
    "discoveryengine.googleapis.com",
    "telemetry.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}
