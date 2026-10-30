# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

locals {
  cloudrun_sa = "serviceAccount:${google_service_account.cloudrun.email}"
}

# =============================================================================
# Build service account: make deploy passes it to gcloud run deploy --source
# (--build-service-account), so source builds do not run as the default
# Compute Engine account
# =============================================================================

resource "google_service_account" "build" {
  account_id   = "genmedia-build"
  display_name = "GenMedia Cloud Run source build"
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

# Cloud Run Builder: read the uploaded source, push the image to Artifact
# Registry and write the build logs (the role Cloud Run documents for a
# source deploy's build service account).
resource "google_project_iam_member" "build" {
  project = var.project_id
  role    = "roles/run.builder"
  member  = "serviceAccount:${google_service_account.build.email}"
}

# =============================================================================
# Dedicated Cloud Run service account (runtime identity)
# =============================================================================

resource "google_service_account" "cloudrun" {
  account_id   = "genmedia-cloudrun"
  display_name = "GenMedia Cloud Run Service Account"
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "cloudrun" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/logging.logWriter",
    "roles/telemetry.tracesWriter",  # ADK traces to telemetry.googleapis.com
    "roles/telemetry.metricsWriter", # ADK metrics (OTLP) to telemetry.googleapis.com
  ])

  project = var.project_id
  role    = each.value
  member  = local.cloudrun_sa
}

# Everything the app stores is in the media bucket (runs/ and sessions/).
resource "google_storage_bucket_iam_member" "cloudrun_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectAdmin"
  member = local.cloudrun_sa
}

# The Studio MCP app signs preview URLs (V4, IAM signBlob) as this account,
# because the Gemini Enterprise iframe can only load storage.googleapis.com.
resource "google_service_account_iam_member" "cloudrun_sign_urls" {
  service_account_id = google_service_account.cloudrun.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.cloudrun_sa
}

# =============================================================================
# Web proxy service account: make web (infra/web_proxy.sh) mints the identity
# token of `gcloud run services proxy` as this account, which works where
# the caller's own credentials cannot mint one (Cloud Shell). make deploy
# grants it roles/run.invoker on the service, which Terraform does not
# create; make share lets teammates act as it.
# =============================================================================

resource "google_service_account" "web" {
  account_id   = "genmedia-web"
  display_name = "GenMedia web proxy: invokes the Cloud Run service only"
  project      = var.project_id

  depends_on = [google_project_service.apis]
}

# The identity running Terraform (the deployer): a user or a service account
data "google_client_openid_userinfo" "deployer" {}

locals {
  # gcloud's account (make), else the credentials' email: Cloud Shell's
  # credentials name none (null)
  deployer_email = try(coalesce(var.deployer_email, data.google_client_openid_userinfo.deployer.email), "")
  deployer_member = (
    endswith(local.deployer_email, ".gserviceaccount.com")
    ? "serviceAccount:${local.deployer_email}"
    : "user:${local.deployer_email}"
  )
}

# Token Creator on this account only: the deployer can mint its tokens
resource "google_service_account_iam_member" "web_deployer" {
  service_account_id = google_service_account.web.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.deployer_member

  lifecycle {
    precondition {
      condition     = local.deployer_email != ""
      error_message = "No deployer account: set TF_VAR_deployer_email (make setup-infra passes gcloud's active account), or run gcloud auth application-default login and try again."
    }
  }
}

# =============================================================================
# Vertex AI service agent: reads the gs:// files the Studio agent passes to
# Gemini from the media bucket (the models return their outputs inline)
# =============================================================================

resource "google_project_service_identity" "aiplatform" {
  provider = google-beta
  project  = var.project_id
  service  = "aiplatform.googleapis.com"

  depends_on = [google_project_service.apis]
}

# Granted on the project with a condition limiting it to the media bucket:
# bucket IAM refuses a just-created service agent ("does not exist") on a
# fresh project, project IAM accepts it.
resource "google_project_iam_member" "aiplatform_media" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_project_service_identity.aiplatform.email}"

  condition {
    title       = "media-bucket-only"
    description = "Read the media bucket only"
    expression  = "resource.name == \"projects/_/buckets/${google_storage_bucket.media.name}\" || resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.media.name}/\")"
  }
}
