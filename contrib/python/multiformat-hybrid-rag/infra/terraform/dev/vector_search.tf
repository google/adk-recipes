
# RAG documents bucket — name sourced from .env GCS_BUCKET
resource "google_storage_bucket" "rag_docs" {
  name                        = var.gcs_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  depends_on = [resource.google_project_service.services]
}

# Vector Search 2.0 collection is created (and re-created) by the pipeline
# itself via vs_utils.ensure_collection() on the first chunk-and-index run.
# This keeps the schema (fields, dims, text_template, embedding model) in
# lockstep with the code that writes to it.

# ---------------------------------------------------------------------------
# Artifact Registry — Docker repo for pipeline container images
# ---------------------------------------------------------------------------
resource "google_artifact_registry_repository" "pipeline" {
  repository_id = "${var.project_name}-pipeline"
  location      = var.region
  project       = var.project_id
  format        = "DOCKER"
  description   = "Docker images for the RAG ingestion pipeline"

  depends_on = [resource.google_project_service.services]
}

# ---------------------------------------------------------------------------
# Pipeline artifacts bucket — GCS root for Vertex AI Pipeline runs
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "pipeline_root" {
  name                        = "${var.project_id}-${var.project_name}-pipeline"
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = true

  depends_on = [resource.google_project_service.services]
}
