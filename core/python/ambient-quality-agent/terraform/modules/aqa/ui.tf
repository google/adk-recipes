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

# A dedicated identity for the UI, like the engine's own: run as this and the
# only grant it carries is `aiplatform.user`, so a UI that only calls the engine
# cannot reach the buckets, datasets and queues the engine SA can.
resource "google_service_account" "ui" {
  project = var.project_id
  # `${local.prefix}-ui` can pass the 30-char account_id limit that `-caller`
  # already sits near, so cut it to length; trimming a trailing hyphen keeps the
  # id from ending in one, which IAM rejects.
  account_id   = trimsuffix(substr("${local.prefix}-ui", 0, 30), "-")
  display_name = "AQA chat UI for ${var.observed_deployment_name}"
}

# Lets the UI call the engine's routes. Publisher models take no IAM
# policy of their own, so there is no smaller scope than the project.
resource "google_project_iam_member" "ui_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.ui.email}"
}

# Grants the UI read, list, and delete access to chat transcripts in the
# jobs bucket. `roles/storage.objectUser` provides `storage.objects.delete`
# alongside get and list without granting full `objectAdmin`.
#
# Because bucket IAM cannot be scoped to a prefix, `task_reader.delete_context`
# restricts operations to `tasks/` to avoid touching investigation run state or
# `aqa-telemetry/`.
resource "google_storage_bucket_iam_member" "ui_jobs_object_user" {
  bucket = google_storage_bucket.jobs.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.ui.email}"
}

# Terraform owns the service's configuration, `agents-cli deploy` owns its code,
# mirroring the engine in service.tf. The env is written as references to the
# resources beside it, the engine's resource name included.
resource "google_cloud_run_v2_service" "ui" {
  # A service cannot turn IAP on before the API that backs it is enabled.
  depends_on = [google_project_service.iap]

  name                = "${local.prefix}-ui"
  location            = var.region
  project             = var.project_id
  deletion_protection = false
  labels              = local.common_labels

  # Unless turned off, the run.app URL is fronted by Google SSO (see the IAP
  # block below) rather than reached through a `gcloud run services proxy`
  # tunnel.
  iap_enabled = var.ui_iap_enabled

  template {
    service_account = google_service_account.ui.email

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      # Cloud Run needs an image to create the service; `agents-cli deploy`
      # replaces it with a source-built image, and `ignore_changes` below stops
      # a later apply from reverting it.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      ports {
        container_port = 8080
      }

      # Match what `agents-cli deploy` (`--no-cpu-throttling`) and `gcloud`
      # (startup CPU boost) configure, preventing applies and deploys from
      # reverting each other. No extra cost while `min_instance_count` is zero.
      resources {
        cpu_idle          = false
        startup_cpu_boost = true
      }

      # `.id` is the full `projects/P/locations/L/reasoningEngines/ID` resource
      # name; `.name` is only the bare id, which leaves the UI's client unable to
      # derive the project and location. ingress.tf uses `.id` for the same reason.
      env {
        name  = "AGENT_ENGINE_RESOURCE_ID"
        value = google_vertex_ai_reasoning_engine.aqa.id
      }

      # Where the agent keeps chat transcripts. Same bucket service.tf hands
      # the engine, because the UI reads back exactly what the engine wrote;
      # unset, the conversation list reports itself unavailable rather than
      # drawing an empty sidebar.
      env {
        name  = "AQA_JOBS_GCS_BUCKET"
        value = google_storage_bucket.jobs.name
      }
    }
  }

  lifecycle {
    ignore_changes = [
      # Built from source by deploy; managing it here would overwrite deployed code.
      template[0].containers[0].image,
      # Set by `gcloud` on deploy; ignored so apply does not clear them.
      client,
      client_version,
      # Added by `agents-cli deploy` (`created-by=agents-cli`); top-level labels
      # are non-authoritative in Google provider 5+, so only template needs ignoring.
      template[0].labels["created-by"],
    ]
  }
}

# --- Identity-Aware Proxy ----------------------------------------------------
# IAP fronts the run.app URL with Google SSO, so a teammate reaches the UI in a
# browser once granted `iap.httpsResourceAccessor` -- no tunnel, no local
# tooling. On by default, because inside a Google organisation the
# Google-managed OAuth client makes it turnkey. A project with no organisation
# ancestor has no managed client and cannot create one programmatically, so it
# sets `ui_iap_enabled = false` and reaches the UI through
# `gcloud run services proxy`. The service stays private either way.

resource "google_project_service" "iap" {
  count = var.ui_iap_enabled ? 1 : 0

  project = var.project_id
  service = "iap.googleapis.com"
  # Other deployments in the project may rely on IAP; never disable it on destroy.
  disable_on_destroy = false
}

# Turning IAP on for the service is what creates the project's IAP service agent.
# Give it time to propagate before the invoker binding below names it, otherwise
# the binding can fail with "service account does not exist".
resource "time_sleep" "iap_service_agent" {
  count = var.ui_iap_enabled ? 1 : 0

  depends_on      = [google_cloud_run_v2_service.ui]
  create_duration = "30s"
}

# IAP intercepts the request and forwards it as its own service agent, which
# therefore must be allowed to invoke the service.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  count = var.ui_iap_enabled ? 1 : 0

  project    = var.project_id
  location   = var.region
  name       = google_cloud_run_v2_service.ui.name
  role       = "roles/run.invoker"
  member     = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"
  depends_on = [time_sleep.iap_service_agent]
}

# Who may open the UI through IAP. Empty lets no one in (grant access later with
# `gcloud iap web add-iam-policy-binding`); list members to provision it here.
resource "google_iap_web_cloud_run_service_iam_member" "ui_accessor" {
  for_each = var.ui_iap_enabled ? toset(var.ui_iap_members) : toset([])

  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.ui.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
  depends_on             = [google_project_service.iap]
}
