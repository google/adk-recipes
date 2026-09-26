resource "google_storage_bucket" "staging" {
  name                        = var.staging_bucket_name
  location                    = var.region
  project                     = var.project_id
  uniform_bucket_level_access = true
  force_destroy               = var.environment != "prod"
  lifecycle_rule {
    condition { age = 90 }
    action { type = "Delete" }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cloud_run_staging" {
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.cloud_run_sa}"
}

resource "google_storage_bucket_iam_member" "agent_engine_staging" {
  count  = var.google_managed_sas_exist ? 1 : 0
  bucket = google_storage_bucket.staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.agent_engine_sa}"
}

resource "google_firestore_database" "main" {
  project     = var.project_id
  name        = var.firestore_database_id
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
  delete_protection_state = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy         = var.environment == "prod" ? "ABANDON" : "DELETE"
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "docker" {
  project       = var.project_id
  location      = var.region
  repository_id = var.ar_repo_name
  format        = "DOCKER"
  description   = "Customer Support MAS Docker images (FastAPI + React)"
  depends_on = [google_project_service.apis]
}
