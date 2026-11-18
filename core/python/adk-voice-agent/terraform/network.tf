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

# The private network the users' computers live on. Computers have no public IP:
# the app reaches them over Direct VPC egress, users reach them through IAP's TCP
# forwarding, and they reach the internet (npm, GitHub, model APIs) through Cloud NAT.

locals {
  # Network tag on every computer; the firewall rules below match it.
  computer_tag = "adk-computer"
  # Also read by deploy.py (`local.deploy`) to find this deployment's computers.
  computer_network_name = "${var.service_name}-net"
  # IAP's TCP forwarding range (`gcloud compute ssh --tunnel-through-iap`).
  iap_tcp_range = "35.235.240.0/20"
}

resource "google_compute_network" "computers" {
  name                    = local.computer_network_name
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [google_project_service.enabled]
}

# One VM per user. Private Google Access lets computers pull from Artifact Registry
# and call Vertex AI without going through NAT.
resource "google_compute_subnetwork" "computers" {
  name                     = "${var.service_name}-computers"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.computers.id
  ip_cidr_range            = var.computer_subnet_cidr
  private_ip_google_access = true
}

# Direct VPC egress for the Cloud Run app (cloud_run.tf). Cloud Run takes its
# instances' addresses from this range, which the firewall trusts on port 8080.
resource "google_compute_subnetwork" "app" {
  name                     = "${var.service_name}-app"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.computers.id
  ip_cidr_range            = var.app_subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_router" "computers" {
  name    = "${var.service_name}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.computers.id
}

resource "google_compute_router_nat" "computers" {
  name                               = "${var.service_name}-nat"
  project                            = var.project_id
  region                             = var.region
  router                             = google_compute_router.computers.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.computers.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# SSH (22) and the daemon (8080) through IAP's TCP forwarding. Who may open a
# tunnel to which computer is decided per instance by IAM, not by this rule.
resource "google_compute_firewall" "iap_to_computers" {
  name          = "${var.service_name}-allow-iap"
  project       = var.project_id
  network       = google_compute_network.computers.id
  direction     = "INGRESS"
  source_ranges = [local.iap_tcp_range]
  target_tags   = [local.computer_tag]

  allow {
    protocol = "tcp"
    ports    = ["22", "8080"]
  }
}

# The app calls each computer's daemon on 8080. The daemon also checks a Google ID
# token from the app's service account, so the network is not the only gate.
resource "google_compute_firewall" "app_to_computers" {
  name          = "${var.service_name}-allow-app"
  project       = var.project_id
  network       = google_compute_network.computers.id
  direction     = "INGRESS"
  source_ranges = [google_compute_subnetwork.app.ip_cidr_range]
  target_tags   = [local.computer_tag]

  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}
