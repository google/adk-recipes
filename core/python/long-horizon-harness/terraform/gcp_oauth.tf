# "Connect Google" OAuth (app/auth/oauth.py). Entirely OPTIONAL:
# with the three vars unset the feature is off (the backend returns 503 from
# /lha/gcp/connect) and nothing here is created — a fresh user gets a working
# deploy without configuring OAuth. Set all three (client id/secret + redirect)
# to turn it on; the env wiring is added to the app service below.

locals {
  gcp_oauth_enabled = (
    var.gcp_oauth_client_id != "" &&
    var.gcp_oauth_client_secret != "" &&
    var.gcp_oauth_redirect_uri != ""
  )
}

resource "google_secret_manager_secret" "gcp_oauth_client_secret" {
  count     = local.gcp_oauth_enabled ? 1 : 0
  secret_id = "${var.service_name}-gcp-oauth-client-secret"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "gcp_oauth_client_secret" {
  count       = local.gcp_oauth_enabled ? 1 : 0
  secret      = google_secret_manager_secret.gcp_oauth_client_secret[0].id
  secret_data = var.gcp_oauth_client_secret
}

# cloud_run already holds project-wide secretmanager.secretAccessor (iam.tf), but
# bind per-secret too for parity with the db secrets and least-surprise.
resource "google_secret_manager_secret_iam_member" "cloud_run_gcp_oauth_client_secret" {
  count     = local.gcp_oauth_enabled ? 1 : 0
  project   = var.project_id
  secret_id = google_secret_manager_secret.gcp_oauth_client_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run.email}"
}
