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

# Runtime service account for the main ADK Voice Agent Cloud Run service (`adk-voice-agent`).
# Manages users' computers (custom role and computer account access in computer.tf).
resource "google_service_account" "app" {
  account_id                   = local.service_account_id
  display_name                 = "ADK Voice Agent runtime"
  project                      = var.project_id
  create_ignore_already_exists = true

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", local.service_account_id))
      error_message = "service_account_id (${local.service_account_id}) must be 6-30 lowercase letters, digits, or hyphens."
    }
  }

  depends_on = [google_project_service.enabled]
}

locals {
  service_account_id = coalesce(var.service_account_id, var.service_name)

  # Custom role IDs are per deployment (a second one in the project would collide, and a
  # deleted ID stays reserved for weeks). role_id allows [a-zA-Z0-9_.], 3-64 chars;
  # service_name's 39-char cap leaves room for the longest suffix.
  role_id_prefix = replace(var.service_name, "-", "_")

  app_roles = concat(
    [
      "roles/aiplatform.user",
      "roles/logging.logWriter",
      "roles/cloudtrace.agent",
    ],
    var.enable_cloud_sql ? ["roles/cloudsql.client"] : [],
  )
}

resource "google_project_iam_member" "app" {
  for_each = toset(local.app_roles)

  project = var.project_id
  role    = each.key
  member  = google_service_account.app.member
}

# `gcloud run deploy --source` builds with the default compute service account,
# which new projects no longer grant build permissions by default.
resource "google_project_iam_member" "cloud_build_builder" {
  count = var.grant_cloud_build_builder ? 1 : 0

  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# Per-user connected-app tokens (app/auth/credential_store.py): one secret per user,
# `<service_name>-user-<hash>`, created by the app the first time that user connects an
# account. The app can create secrets, but can only read, add versions to, and
# destroy versions of `<service_name>-user-*` secrets, not the deployment's other secrets.
locals {
  user_secret_prefix = "projects/${data.google_project.current.number}/secrets/${var.service_name}-user-"
}

# `secretmanager.secrets.create` is checked against the project, not the new secret's
# name, so it can't carry the name condition below. Creating a secret grants no
# access to existing ones, and an existing name is refused (AlreadyExists).
resource "google_project_iam_custom_role" "user_secret_creator" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_user_secret_creator"
  title       = "ADK Voice Agent per-user secret creator"
  description = "Create the per-user credential secrets ADK Voice Agent keeps for each signed-in user."
  permissions = ["secretmanager.secrets.create"]
}

resource "google_project_iam_custom_role" "user_secret_editor" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_user_secret_editor"
  title       = "ADK Voice Agent per-user secret editor"
  description = "Read and replace versions of ADK Voice Agent's per-user credential secrets."
  permissions = [
    "secretmanager.versions.access",
    "secretmanager.versions.add",
    "secretmanager.versions.destroy",
    "secretmanager.versions.get",
  ]
}

resource "google_project_iam_member" "app_user_secret_creator" {
  project = var.project_id
  role    = google_project_iam_custom_role.user_secret_creator.id
  member  = google_service_account.app.member
}

resource "google_project_iam_member" "app_user_secret_editor" {
  project = var.project_id
  role    = google_project_iam_custom_role.user_secret_editor.id
  member  = google_service_account.app.member

  condition {
    title       = "voice-agent-user-secrets-only"
    description = "Only ADK Voice Agent's per-user credential secrets."
    expression  = "resource.name.startsWith(\"${local.user_secret_prefix}\")"
  }
}
