# ==============================================================================
# Data sources and local values
# ==============================================================================

data "google_project" "project" {
  project_id = var.project_id
}

locals {
  project_number = data.google_project.project.number
  agent_engine_sa = "service-${local.project_number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  vertex_sa       = "service-${local.project_number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
  cloud_run_sa    = "${local.project_number}-compute@developer.gserviceaccount.com"
  cloud_build_sa  = "${local.project_number}@cloudbuild.gserviceaccount.com"
  ar_base_url     = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo_name}"
}
