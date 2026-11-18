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

# Deployment-wide credentials live in Secret Manager, one secret per key, never in plain
# Cloud Run env vars. Terraform creates the empty secrets; values are added out-of-band
# (`make onboard` saves them to .env, and scripts/deploy.py copies them from there), so
# they never pass through tfvars or terraform state.

locals {
  app_configs = values(local.apps) # main.tf

  # Deployment-wide keys that aren't in integrations.yaml: the GitHub App's URL name, which
  # `make onboard` saves with its client (app/auth/github/provider.py).
  deployment_env_keys = ["GITHUB_APP_SLUG"]

  # Each app's OAuth client, registered by `make onboard`, and the keys above.
  stored_credential_env_keys = setunion(compact(flatten([
    for app in local.app_configs : [
      lookup(app, "client_id_env", ""),
      lookup(app, "client_secret_env", ""),
    ]
  ])), local.deployment_env_keys)

  # Every credential the app reads from the environment; none is deployed as a plain env
  # var. Users' tokens are per user: each user connects their own accounts in the app,
  # which keeps them in that user's `voice-agent-user-<hash>` secret
  # (app/auth/credential_store.py; IAM in iam.tf), so they're never deployed at all.
  credential_env_keys = setunion(compact(flatten([
    for app in local.app_configs : [
      lookup(app, "access_token_env", ""),
      lookup(app, "refresh_token_env", ""),
      lookup(app, "token_source_env", ""),
      lookup(app, "granted_scopes_env", ""),
      lookup(app, "account_label_env", ""),
    ]
  ])), local.stored_credential_env_keys)

  # GOOGLE_OAUTH_CLIENT_ID -> adk-voice-agent-google-oauth-client-id
  credential_secrets = {
    for key in local.stored_credential_env_keys :
    key => "${var.service_name}-${lower(replace(key, "_", "-"))}"
  }
}

resource "google_secret_manager_secret" "credential" {
  for_each = local.credential_secrets

  secret_id = each.value
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_iam_member" "credential" {
  for_each = google_secret_manager_secret.credential

  project   = var.project_id
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}
