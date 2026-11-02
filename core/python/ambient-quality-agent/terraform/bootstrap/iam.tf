# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Reads through Cloud Resource Manager, which this root enables.
data "google_project" "this" {
  project_id = var.project_id

  depends_on = [google_project_service.bootstrap]
}

# gcloud run deploy --source . builds the UI as this account, which has no roles
# under the org policy iam.automaticIamGrantsForDefaultServiceAccounts.
resource "google_project_iam_member" "default_compute_cloudbuild_builder" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.builder"
  member  = "serviceAccount:${data.google_project.this.number}-compute@developer.gserviceaccount.com"

  # Enabling run.googleapis.com is what creates this account. Wait out the
  # propagation delay before naming it, otherwise the grant can fail with
  # "service account does not exist".
  depends_on = [time_sleep.propagation]
}
