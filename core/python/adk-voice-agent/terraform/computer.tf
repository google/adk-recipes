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

# Users' computers: terraform owns deployment-wide resources (repo, SA, template,
# snapshot schedule); the app creates one VM (`<service_name>-u-<hash>`) and home disk
# (`<service_name>-u-<hash>-home`) per user at runtime.

locals {
  computer_zone               = coalesce(var.computer_zone, "${var.region}-a")
  computer_service_account_id = coalesce(var.computer_service_account_id, "${var.service_name}-computer")
  registry_host               = "${var.region}-docker.pkg.dev"
  computer_repo_url           = "${local.registry_host}/${var.project_id}/${var.service_name}"
  computer_image              = coalesce(var.computer_image, "${local.computer_repo_url}/computer:latest")

  # Device name of the user's home disk, as the provider attaches it; the guest
  # sees it at /dev/disk/by-id/google-<name>.
  computer_disk_device = "home"

  computer_user_data = templatefile("${path.module}/computer-cloud-init.yaml.tftpl", {
    image            = local.computer_image
    registry         = local.registry_host
    disk_device_name = local.computer_disk_device
    home_mount       = "/mnt/disks/home"
  })

  # Changes whenever anything that shapes a computer changes. Set as the `template`
  # label on the template's instances; a computer whose label differs is recreated
  # from the current template (its disk is kept).
  computer_template_label = substr(sha256(jsonencode({
    image        = local.computer_image
    machine_type = var.computer_machine_type
    user_data    = local.computer_user_data
    sa           = google_service_account.computer.email
  })), 0, 16)
}

# The computer image (`make computer-image` builds sandbox/Dockerfile into it).
resource "google_artifact_registry_repository" "images" {
  repository_id = var.service_name
  project       = var.project_id
  location      = var.region
  format        = "DOCKER"
  description   = "ADK Voice Agent computer image"

  depends_on = [google_project_service.enabled]
}

# What the coding agents on a computer run as: Vertex AI models, logs, and
# pulling the computer image. Nothing else: no Secret Manager (users' tokens),
# no Cloud SQL, no compute.
resource "google_service_account" "computer" {
  account_id                   = local.computer_service_account_id
  display_name                 = "ADK Voice Agent computers (coding agents)"
  project                      = var.project_id
  create_ignore_already_exists = true

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", local.computer_service_account_id))
      error_message = "computer_service_account_id (${local.computer_service_account_id}) must be 6-30 lowercase letters, digits, or hyphens (set computer_service_account_id when service_name exceeds 21 chars)."
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "computer" {
  for_each = toset(["roles/aiplatform.user", "roles/logging.logWriter"])

  project = var.project_id
  role    = each.key
  member  = google_service_account.computer.member
}

resource "google_artifact_registry_repository_iam_member" "computer_pull" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.computer.member
}

# The daemon's MCP relay calls the app's bridge through IAP as this account
# (COMPUTER_MCP_AUDIENCE). Cloud Run's IAP uses a Google-managed OAuth client,
# which accepts service-account JWTs signed through the IAM Credentials API
# (`signJwt`, aud = the app URL), so the account may sign JWTs as itself only.
resource "google_iap_web_cloud_run_service_iam_member" "computer" {
  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.app.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = google_service_account.computer.member
}

resource "google_service_account_iam_member" "computer_sign_self" {
  service_account_id = google_service_account.computer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.computer.member
}

# Daily snapshots of every home disk, kept a week. The provider attaches it to
# each disk it creates (COMPUTER_SNAPSHOT_POLICY).
resource "google_compute_resource_policy" "home_snapshots" {
  name    = "${var.service_name}-home-snapshots"
  project = var.project_id
  region  = var.region

  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "08:00"
      }
    }
    retention_policy {
      max_retention_days    = 7
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      labels = {
        goog-adk-computer = "true"
        deployment        = var.service_name
      }
      storage_locations = [var.region]
    }
  }

  depends_on = [google_project_service.enabled]
}

# Every computer is created from this template (COMPUTER_INSTANCE_TEMPLATE).
# Templates are immutable, so a change makes a new one (name_prefix) before the old
# one goes; running computers pick it up on their next start.
resource "google_compute_instance_template" "computer" {
  name_prefix  = "${var.service_name}-computer-"
  project      = var.project_id
  region       = var.region
  machine_type = var.computer_machine_type
  tags         = [local.computer_tag]

  # Instances inherit these; the provider sets the same `deployment` on home disks.
  labels = {
    goog-adk-computer = "true"
    deployment        = var.service_name
    template          = local.computer_template_label
  }

  # Boot disk only; the provider attaches the user's home disk at insert time.
  disk {
    source_image = "projects/cos-cloud/global/images/family/cos-stable"
    disk_type    = "pd-balanced"
    disk_size_gb = 20
    auto_delete  = true
    boot         = true
  }

  # No access_config: no public IP. Outbound goes through Cloud NAT.
  network_interface {
    subnetwork = google_compute_subnetwork.computers.id
  }

  service_account {
    email  = google_service_account.computer.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  scheduling {
    automatic_restart   = true
    on_host_maintenance = "MIGRATE"
  }

  metadata = {
    user-data              = local.computer_user_data
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    google-logging-enabled = "true"
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    google_project_iam_member.computer,
    google_artifact_registry_repository_iam_member.computer_pull,
  ]
}

# ---- What the app's service account may do with computers ----

# Project-wide permissions checked before an instance or disk exists (or on
# list/template/zone operations). Per-instance mutations live in
# computer_instance_manager below, conditioned on `<service_name>-u-*`.
resource "google_project_iam_custom_role" "computer_manager" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_computer_manager"
  title       = "ADK Voice Agent computer manager"
  description = "Create and list the per-user computers ADK Voice Agent creates from its instance template."
  permissions = [
    "compute.instances.create",
    "compute.instances.list",
    # Creating a disk with its labels and snapshot policy is checked before the disk
    # exists, so like user_secret_creator (iam.tf) it stays project-wide; none of it
    # can read, attach, or change an existing disk's data. Access to existing
    # disks: computer_disk_manager.
    "compute.disks.create",
    "compute.disks.setLabels",
    "compute.disks.addResourcePolicies",
    "compute.disks.list",
    "compute.resourcePolicies.use",
    "compute.instanceTemplates.get",
    "compute.instanceTemplates.useReadOnly",
    "compute.images.useReadOnly",
    "compute.subnetworks.use",
    "compute.zoneOperations.get",
    "compute.zoneOperations.list", # zoneOperations.wait checks .get
  ]
}

resource "google_project_iam_member" "app_computer_manager" {
  project = var.project_id
  role    = google_project_iam_custom_role.computer_manager.id
  member  = google_service_account.app.member
}

# Per-instance permissions (start, stop, delete, metadata/labels/IAM, and IAP
# tunnel IAM for SSH), granted only on this deployment's computers (`<service_name>-u-*`).
resource "google_project_iam_custom_role" "computer_instance_manager" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_computer_vm_manager"
  title       = "ADK Voice Agent computer instance manager"
  description = "Manage and grant SSH to ADK Voice Agent's per-user computers."
  permissions = [
    "compute.instances.delete",
    "compute.instances.get",
    "compute.instances.start",
    "compute.instances.stop",
    "compute.instances.resume",
    "compute.instances.setMetadata",
    "compute.instances.setLabels",
    "compute.instances.setTags",
    "compute.instances.setServiceAccount",
    "compute.instances.attachDisk",
    "compute.instances.setDiskAutoDelete",
    "compute.instances.getIamPolicy",
    "compute.instances.setIamPolicy",
    "iap.tunnelInstances.getIamPolicy",
    "iap.tunnelInstances.setIamPolicy",
  ]
}

resource "google_project_iam_member" "app_computer_instance_manager" {
  project = var.project_id
  role    = google_project_iam_custom_role.computer_instance_manager.id
  member  = google_service_account.app.member
  condition {
    title       = "this-deployments-computers"
    description = "Only ${var.service_name}'s per-user computers."
    expression  = "resource.name.extract(\"/instances/{name}\").startsWith(\"${var.service_name}-u-\")"
  }
}

# Existing disks are granted apart, and only this deployment's own: each computer's
# boot disk (`<service_name>-u-<hash>`) and home disk (`<service_name>-u-<hash>-home`).
# The app can't read, attach, or change any other disk in the project.
resource "google_project_iam_custom_role" "computer_disk_manager" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_computer_disk_manager"
  title       = "ADK Voice Agent computer disk manager"
  description = "Read, attach, and keep the disks of ADK Voice Agent's computers."
  permissions = [
    "compute.disks.get",
    "compute.disks.use",
    "compute.disks.update", # setDiskAutoDelete checks it on the home disk
  ]
}

resource "google_project_iam_member" "app_computer_disk_manager" {
  project = var.project_id
  role    = google_project_iam_custom_role.computer_disk_manager.id
  member  = google_service_account.app.member
  condition {
    title       = "this-deployments-computer-disks"
    description = "Only the disks of ${var.service_name}'s computers."
    expression  = "resource.type == \"compute.googleapis.com/Disk\" && resource.name.extract(\"/disks/{name}\").startsWith(\"${var.service_name}-u-\")"
  }
}

# Computers run as the computer account, so the app must be able to act as it.
resource "google_service_account_iam_member" "app_uses_computer_sa" {
  service_account_id = google_service_account.computer.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.app.member
}

# SSH to a computer also needs serviceAccountUser on the account it runs as; the
# app grants that to each user once. This role lets it edit that one account's
# policy and nothing else.
resource "google_project_iam_custom_role" "computer_sa_access_admin" {
  project     = var.project_id
  role_id     = "${local.role_id_prefix}_computer_sa_access_admin"
  title       = "ADK Voice Agent computer account access admin"
  description = "Grant users serviceAccountUser on the computer service account, for SSH."
  permissions = [
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.getIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
  ]
}

resource "google_service_account_iam_member" "app_computer_sa_access_admin" {
  service_account_id = google_service_account.computer.name
  role               = google_project_iam_custom_role.computer_sa_access_admin.id
  member             = google_service_account.app.member
}
