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

resource "google_cloud_run_v2_service" "app" {
  name                = var.project_name
  location            = var.region
  project             = var.project_id
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels = {
    "created-by" = "adk"
  }

  template {
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits = {
          cpu    = "4"
          memory = "8Gi"
        }
      }
      # The image ships .env.example as a defaults file, and it necessarily
      # carries a placeholder project id. Without these two the container
      # resolves GOOGLE_CLOUD_PROJECT to that placeholder and every Vertex AI
      # call fails with PERMISSION_DENIED on a project that isn't yours.
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }

      env {
        name  = "VECTOR_SEARCH_COLLECTION"
        value = "projects/${var.project_id}/locations/${var.region}/collections/${var.vs_collection_id}"
      }

      env {
        name  = "VECTOR_SEARCH_DOCUMENTS_COLLECTION"
        value = "projects/${var.project_id}/locations/${var.region}/collections/${var.vs_documents_collection_id}"
      }

      # The agent picks its MCP transport at import time, which happens
      # before fast_api_app.py can set this. Injecting it here is what makes
      # the served agent use the mounted /mcp SSE app instead of spawning a
      # duplicate MCP server as a stdio subprocess. Left unset locally, so
      # `adk web` / `make playground` still get stdio.
      env {
        name  = "MCP_SERVER_URL"
        value = "http://localhost:8080/mcp/sse"
      }

      env {
        name  = "GOOGLE_CLOUD_LOCATION_MODELS"
        value = var.model_location
      }

      env {
        name  = "LOGS_BUCKET_NAME"
        value = google_storage_bucket.logs_data_bucket.name
      }

      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }
    }

    service_account                  = google_service_account.app_sa.email
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }

    session_affinity = true
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # This lifecycle block prevents Terraform from overwriting the container image when it's
  # updated by Cloud Run deployments outside of Terraform (e.g., via CI/CD pipelines)
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }

  # Make dependencies conditional to avoid errors.
  depends_on = [
    resource.google_project_service.services,
  ]
}
