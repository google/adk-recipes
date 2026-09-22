# Chunk-and-index Cloud Run service.
#
# Replaces the BigFrames-based chunk_and_index path. Each request handles
# one file end-to-end: split + Gemini contextual enrichment + VS2 chunk
# push + VS2 documents upsert. The pipeline orchestrator dispatches files
# in parallel via authenticated HTTP — same shape as preprocess_service.
#
# Architecture:
#   Pipeline orchestrator (ThreadPoolExecutor, N workers)
#     → Cloud Run (autoscales, langchain split + Gemini + VS2)
#     → JSON of chunk rows back to orchestrator → INSERT into BQ.
#
# Why no BQ remote function: same reasoning as preprocess. BQ assigns
# parallelism by data size, capping us at ~20 instances even on 1k rows.
# Direct fanout from the orchestrator gives deterministic N-way parallelism.

# ---------------------------------------------------------------------------
# Cloud Run Service
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "chunk_index" {
  name                = "${var.project_name}-chunk-index"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.vertexai_pipeline_app_sa["dev"].email

    # One request per instance: forces the autoscaler to spawn N instances
    # for N concurrent requests. Combined with max_instance_count, this
    # caps in-flight parallelism deterministically.
    max_instance_request_concurrency = 1

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # Modest resources — workload is I/O-bound (Gemini + VS2 calls).
      # Bumped from defaults so the in-process ThreadPoolExecutor (16
      # threads enriching chunks in parallel) doesn't become a CPU
      # bottleneck on documents with many small chunks.
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "REGION"
        value = var.region
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION_MODELS"
        value = var.model_location
      }
      env {
        name  = "CONTEXTUAL_CHUNKING_GEMINI_MODEL"
        value = var.contextual_chunking_gemini_model
      }
      env {
        name  = "VS_COLLECTION_PATH"
        value = "projects/${var.project_id}/locations/${var.region}/collections/${var.vs_collection_id}"
      }
      env {
        name  = "VS_DOCUMENTS_COLLECTION_PATH"
        value = "projects/${var.project_id}/locations/${var.region}/collections/${var.vs_documents_collection_id}"
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 200
    }

    timeout = "900s"
  }

  depends_on = [google_project_service.services]

  # Image is updated via Cloud Build / gcloud after initial deployment.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# IAM — allow the pipeline service account to invoke the Cloud Run service.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service_iam_member" "chunk_index_pipeline_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.chunk_index.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.vertexai_pipeline_app_sa["dev"].email}"
}
