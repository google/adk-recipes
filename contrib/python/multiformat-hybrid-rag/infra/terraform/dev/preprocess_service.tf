# Preprocessing Cloud Run service.
#
# A Docker image with LibreOffice + Gemini SDK that extracts text and
# classifies relevance for a single GCS file per request. The pipeline
# orchestrator dispatches files in parallel via authenticated HTTP — no
# BQ remote function in the loop.
#
# Architecture:
#   Pipeline orchestrator (ThreadPoolExecutor, N workers)
#     → Cloud Run (autoscales, LibreOffice + Gemini per instance)
#     → JSON result back to orchestrator → MERGE into BQ.
#
# Why no BQ remote function: BQ assigns parallelism by data size, so for
# small inputs it serializes calls onto a single slot, blocking that slot
# for the entire HTTP round-trip. Direct fanout from the orchestrator
# gives deterministic N-way parallelism.

# ---------------------------------------------------------------------------
# Cloud Run Service
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "preprocess" {
  name                = "${var.project_name}-preprocess"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  # Public-routable but IAM-locked: only callers with run.invoker on this
  # service can hit it. Allows local + Vertex AI Pipelines orchestrators
  # to reach the service over the internet (they aren't in the VPC).
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    # Reuse the pipeline service account — it already has GCS read,
    # Vertex AI (Gemini), and BigQuery permissions.
    service_account = google_service_account.vertexai_pipeline_app_sa["dev"].email

    # Two requests per instance. concurrency=1 hit Cloud Run's instance-start
    # rate limit at ~110 active even with max_instance_count=200 (575 platform
    # 429s "no available instance" in the 1k-file benchmark). Letting each
    # instance handle 2 requests doubles effective parallelism without
    # forcing more cold starts. Per-request CPU/RAM headroom (4 vCPU / 8 GiB)
    # easily covers two simultaneous LibreOffice + Gemini workloads since
    # Gemini latency dominates and is mostly network wait.
    max_instance_request_concurrency = 2

    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      # High resources for processing large documents (PDF page splitting,
      # LibreOffice conversion, Gemini API calls)
      resources {
        limits = {
          cpu    = "4"
          memory = "8Gi"
        }
      }

      # Configuration passed to the FastAPI app via environment variables.
      # These are read by main.py at startup to initialize GCS and Gemini clients.
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
        name  = "GCS_BUCKET"
        value = var.gcs_bucket
      }
      env {
        name  = "MARKDOWN_CONVERTER_GEMINI_MODEL"
        value = var.markdown_converter_gemini_model
      }
      env {
        name  = "RELEVANCE_GEMINI_MODEL"
        value = var.relevance_gemini_model
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = 100
    }

    # 15 minute timeout for large documents (e.g. 100+ page PDFs)
    timeout = "900s"
  }

  depends_on = [google_project_service.services]

  # After initial deployment, the image is updated via Cloud Build / gcloud,
  # not Terraform. This prevents Terraform from reverting to a stale image tag.
  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
    ]
  }
}

# ---------------------------------------------------------------------------
# IAM — allow the pipeline service account to invoke the Cloud Run service.
# ---------------------------------------------------------------------------
# The pipeline component (whether running on Vertex AI Pipelines or locally
# with the developer's own ADC) authenticates with this SA's identity and
# mints an ID token whose audience is the Cloud Run URL.
resource "google_cloud_run_v2_service_iam_member" "preprocess_pipeline_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.preprocess.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.vertexai_pipeline_app_sa["dev"].email}"
}
