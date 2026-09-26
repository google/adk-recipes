# ==============================================================================
# IAM — google_project_iam_member (additive, does not replace existing bindings)
# ==============================================================================

# ------------------------------------------------------------------------------
# Vertex AI Agent Engine service account
# Runs deployed agents; needs Firestore to call tools and Vertex AI for Gemini.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "agent_engine_sa" {
  for_each = var.google_managed_sas_exist ? toset([
    "roles/datastore.user",       # Read/write Firestore (tool calls)
    "roles/aiplatform.user",      # Call Gemini / Vertex AI APIs
    "roles/storage.objectViewer", # Read staging bucket artifacts
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.agent_engine_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Core Vertex AI service account
# Used for embeddings and direct generateContent calls.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "vertex_sa" {
  for_each = var.google_managed_sas_exist ? toset([
    "roles/datastore.user",  # Firestore vector search for RAG
    "roles/aiplatform.user",
  ]) : toset([])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.vertex_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Cloud Run default compute service account
# Runs the FastAPI backend; needs Agent Engine and Firestore.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "cloud_run_sa" {
  for_each = toset([
    # Cloud Run (FastAPI backend) roles
    "roles/aiplatform.user",              # Call Agent Engine
    "roles/datastore.user",               # Read/write sessions and messages
    # CI/CD roles — compute SA is also used as the 2nd gen Cloud Build trigger SA
    # (Google-managed @cloudbuild SA is rejected by 2nd gen builds)
    "roles/aiplatform.admin",             # Deploy to Agent Engine
    "roles/artifactregistry.writer",      # Push Docker images
    "roles/run.admin",                    # Deploy Cloud Run service
    "roles/secretmanager.secretAccessor", # Read staging-bucket secret
    "roles/cloudbuild.builds.editor",     # Invoke Cloud Build triggers (Cloud Scheduler)
    "roles/iam.serviceAccountUser",       # Act as itself during Cloud Run deploy
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

# Cloud Run SA also needs access to the staging bucket at the bucket level
# (granted in infrastructure.tf on the bucket resource itself)

# ------------------------------------------------------------------------------
# Cloud Build service account
# Runs CI/CD pipelines; needs to deploy to Cloud Run and Artifact Registry.
# ------------------------------------------------------------------------------
resource "google_project_iam_member" "cloud_build_sa" {
  for_each = toset([
    "roles/datastore.user",                 # Firestore access during agent eval tests
    "roles/aiplatform.user",                # Call Vertex AI Gemini during eval tests
    "roles/aiplatform.admin",               # Deploy to Agent Engine
    "roles/artifactregistry.writer",        # Push Docker images
    "roles/run.admin",                      # Deploy Cloud Run service
    "roles/storage.objectAdmin",            # Read/write staging bucket
    "roles/secretmanager.secretAccessor",   # Read staging-bucket secret
  ])

  project = var.project_id
  role    = each.key
  member  = "serviceAccount:${local.cloud_build_sa}"

  depends_on = [google_project_service.apis]
}

# Compute SA (used by Cloud Build triggers) needs read/write access to the
# Terraform state bucket so terraform-plan and terraform-apply can run.
resource "google_storage_bucket_iam_member" "compute_sa_tfstate" {
  count  = var.github_connected ? 1 : 0
  bucket = local.tfstate_bucket
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

# Cloud Build SA needs to impersonate the Cloud Run compute SA when deploying
resource "google_service_account_iam_member" "cloud_build_impersonate_compute" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.cloud_run_sa}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${local.cloud_build_sa}"

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------------------------
# Model Armor — grant modelarmor.user to both Vertex AI service accounts
# so that Agent Engine and embedding calls can pass through Model Armor screening
# ------------------------------------------------------------------------------
# Cloud Run SA calls Model Armor API directly from the FastAPI backend
resource "google_project_iam_member" "model_armor_cloud_run" {
  count = var.model_armor_enabled ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.cloud_run_sa}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "model_armor_agent_engine" {
  count = var.model_armor_enabled && var.google_managed_sas_exist ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.agent_engine_sa}"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "model_armor_vertex" {
  count = var.model_armor_enabled && var.google_managed_sas_exist ? 1 : 0

  project = var.project_id
  role    = "roles/modelarmor.user"
  member  = "serviceAccount:${local.vertex_sa}"

  depends_on = [google_project_service.apis]
}
