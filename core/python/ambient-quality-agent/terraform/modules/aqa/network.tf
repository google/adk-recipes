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

# The private path from the engine to the Cloud SQL instance, created only when
# `storage_backend` is `cloudsql`:
#
#   engine --PSC interface--> network attachment --> VPC --peering--> instance
#
# The engine runs in a Google-managed network with no route into this project.
# A PSC interface gives it a NIC in the attachment's subnet. The instance has
# only a private IP (the organization enforces `sql.restrictPublicIp`), which
# private services access places in a range peered into the same VPC.
#
# With the interface, the engine still reaches Google APIs, but not the public
# internet: the VPC has no egress path for it.

locals {
  cloudsql       = var.storage_backend == "cloudsql"
  create_network = local.cloudsql && var.network == null

  network_id = (
    !local.cloudsql ? null :
    var.network != null ? var.network :
    google_compute_network.db[0].id
  )
}

resource "google_compute_network" "db" {
  count = local.create_network ? 1 : 0

  project                 = var.project_id
  name                    = local.prefix
  auto_create_subnetworks = false
}

# Holds the engine's PSC interface. Agent Runtime needs at least a /28 per
# project, and the range must not overlap anything already in the network.
resource "google_compute_subnetwork" "engine_attachment" {
  count = local.cloudsql ? 1 : 0

  project       = var.project_id
  name          = "${local.prefix}-engine"
  region        = var.region
  network       = local.network_id
  ip_cidr_range = var.network_attachment_subnet_range

  # To ensure access to Cloud APIs.
  private_ip_google_access = true
}

# The platform releases an engine's endpoint here some minutes after the engine
# is deleted, and until then the attachment refuses deletion, so a destroy that
# takes both fails on it once.
resource "google_compute_network_attachment" "engine" {
  count = local.cloudsql ? 1 : 0

  project               = var.project_id
  name                  = local.prefix
  region                = var.region
  connection_preference = "ACCEPT_AUTOMATIC"
  subnetworks           = [google_compute_subnetwork.engine_attachment[0].self_link]
}

# Private services access: Google allocates the instance's private IP from this
# range. Cloud SQL takes a free /24 for each region it serves, and one instance
# needs one region.
resource "google_compute_global_address" "cloudsql" {
  count = local.cloudsql ? 1 : 0

  project       = var.project_id
  name          = "${local.prefix}-cloudsql"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 24
  network       = local.network_id
}

# One connection per network and service. In an existing network that already
# has one, creating this fails; the provider can instead overwrite the
# connection's ranges (`update_on_creation_fail`), which would drop the ranges
# other services there rely on, so it is not set.
#
# Deleting the connection is refused for four days after the instance is
# deleted, while Cloud SQL keeps its subnet, and the network cannot be deleted
# while the peering exists. `ABANDON` would leave the peering behind and block
# the network too. `REMOVE_PEERING` removes the peering when the connection is
# refused. That cuts off every instance behind the connection, so it relies on
# the module's instance being deleted first (`depends_on` in `database.tf`) and
# on nothing else in a network passed as `network` using this connection.
resource "google_service_networking_connection" "cloudsql" {
  count = local.cloudsql ? 1 : 0

  network                 = local.network_id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.cloudsql[0].name]
  deletion_policy         = "REMOVE_PEERING"
}

# The Agent Platform service agent accepts the attachment on the engine's
# behalf when the engine is created. These are the permissions the PSC interface
# guide lists in place of `roles/compute.networkAdmin`.
#
# A deleted custom role is undeleted by the next apply for 7 days, and its id
# then stays unusable until up to 37 days after the deletion, so recreating a
# deployment under the same name in that window fails here.
resource "google_project_iam_custom_role" "network_attachment_user" {
  count = local.cloudsql ? 1 : 0

  project     = var.project_id
  role_id     = "${replace(local.prefix, "-", "_")}_psc_interface"
  title       = "PSC interface for ${local.prefix}"
  description = "Lets the Agent Platform service agent attach the engine of ${local.prefix} to its network attachment."
  permissions = [
    "compute.networkAttachments.get",
    "compute.networkAttachments.update",
    "compute.regionOperations.get",
  ]
}

# Project-wide: the binding reaches every network attachment in the project,
# not only this deployment's.
resource "google_project_iam_member" "network_attachment_user" {
  count = local.cloudsql ? 1 : 0

  project = var.project_id
  role    = google_project_iam_custom_role.network_attachment_user[0].id
  member  = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform.iam.gserviceaccount.com"
}
