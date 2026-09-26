locals {
  base_apis = [
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",
  ]
  model_armor_apis = var.model_armor_enabled ? [
    "modelarmor.googleapis.com",
    "dlp.googleapis.com",
  ] : []
  all_apis = concat(local.base_apis, local.model_armor_apis)
}

resource "google_project_service" "apis" {
  for_each = toset(local.all_apis)
  project = var.project_id
  service = each.key
  disable_on_destroy         = false
  disable_dependent_services = false
}
