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

# One service serves the UI and the API. Terraform owns its shape (identity, IAP,
# scaling, Cloud SQL socket); `agents-cli deploy` (scripts/deploy.py) owns the image
# and the environment, so terraform ignores both after the first revision.
resource "google_cloud_run_v2_service" "app" {
  name                = var.service_name
  location            = var.region
  project             = var.project_id
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true
  deletion_protection = false

  template {
    service_account = google_service_account.app.email

    # Live voice sessions are long-lived WebSockets; 3600s is the Cloud Run maximum.
    timeout = "3600s"

    # Single warm instance: live sessions and the in-process thread registry cannot split across instances.
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
    max_instance_request_concurrency = 40

    # Direct VPC egress for private ranges only (reaches computers on internal IPs).
    vpc_access {
      network_interfaces {
        network    = google_compute_network.computers.id
        subnetwork = google_compute_subnetwork.app.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    dynamic "volumes" {
      for_each = var.enable_cloud_sql ? [1] : []
      content {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.db[0].connection_name]
        }
      }
    }

    containers {
      image = var.placeholder_image

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
        # Background threads keep running between requests.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      dynamic "volume_mounts" {
        for_each = var.enable_cloud_sql ? [1] : []
        content {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }
    }
  }

  depends_on = [
    google_project_service.enabled,
    google_project_iam_member.app,
  ]

  lifecycle {
    ignore_changes = [
      client,
      client_version,
      labels,
      template[0].labels,
      template[0].containers[0].image,
      template[0].containers[0].env,
    ]
  }
}

# IAP calls the service on behalf of signed-in users.
resource "google_cloud_run_v2_service_iam_member" "iap_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-iap.iam.gserviceaccount.com"
}

data "google_client_openid_userinfo" "deployer" {}

locals {
  deployer_email = data.google_client_openid_userinfo.deployer.email
  deployer_member = (
    endswith(local.deployer_email, ".gserviceaccount.com")
    ? "serviceAccount:${local.deployer_email}"
    : "user:${local.deployer_email}"
  )
  iap_members = toset(concat(
    var.iap_users,
    var.grant_deployer_access && local.deployer_email != "" ? [local.deployer_member] : [],
  ))
}

resource "google_iap_web_cloud_run_service_iam_member" "users" {
  for_each = local.iap_members

  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.app.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.key
}
