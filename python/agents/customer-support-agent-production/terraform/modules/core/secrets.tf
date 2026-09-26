resource "google_secret_manager_secret" "staging_bucket" {
  project   = var.project_id
  secret_id = "staging-bucket"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "staging_bucket" {
  secret      = google_secret_manager_secret.staging_bucket.id
  secret_data = var.staging_bucket_name
  lifecycle { ignore_changes = [secret_data] }
}
resource "google_secret_manager_secret_iam_member" "cloud_build_staging_bucket" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.staging_bucket.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.cloud_build_sa}"
  depends_on = [google_project_service.apis]
}
