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

variable "project_id" {
  type        = string
  description = "GCP project to deploy into. `make deploy` passes it (PROJECT=, GOOGLE_CLOUD_PROJECT in .env, or your gcloud config)."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Cloud SQL, the computers' network, and the image repository."
  default     = "us-central1"
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name. Must match the agents-cli project name (`name` in agents-cli-manifest.yaml), which is what `agents-cli deploy` deploys to. Also names this deployment's computers (`<service_name>-u-<hash>-home`) and custom roles."
  default     = "adk-voice-agent"

  validation {
    # Computer disks are `<service_name>-u-<16 hex>-home`: 24 chars past the name, under GCE's 63.
    condition     = can(regex("^[a-z]([-a-z0-9]{0,37}[a-z0-9])?$", var.service_name))
    error_message = "service_name must be 1-39 lowercase letters, digits, or hyphens, start with a letter, and not end with a hyphen."
  }
}

variable "service_account_id" {
  type        = string
  description = "Runtime service account id. Empty: `<service_name>`. Manages users' computers (computer.tf)."
  default     = ""

  validation {
    condition     = var.service_account_id == "" || can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", var.service_account_id))
    error_message = "service_account_id must be 6-30 lowercase letters, digits, or hyphens, or empty to use service_name."
  }
}

variable "iap_users" {
  type        = list(string)
  description = "Principals allowed through IAP, as IAM members (\"user:alice@example.com\", \"group:team@example.com\"). Whoever runs terraform is added automatically while grant_deployer_access is true."
  default     = []
}

variable "grant_deployer_access" {
  type        = bool
  description = "Let the account running terraform through IAP, so the first `make deploy` is usable without extra steps."
  default     = true
}

variable "grant_cloud_build_builder" {
  type        = bool
  description = "Grant the default compute service account roles/cloudbuild.builds.builder so `gcloud run deploy --source` works in new projects."
  default     = true
}

variable "placeholder_image" {
  type        = string
  description = "Image for the first revision only. `agents-cli deploy` rolls the real image; terraform ignores image changes afterwards."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "enable_workspace_apis" {
  type        = bool
  description = "Enable the APIs and MCP servers the Google app reaches (Calendar, Gmail, Drive, Docs, Sheets, Slides)."
  default     = true
}

variable "enable_cloud_sql" {
  type        = bool
  description = "Keep thread history and per-user settings in Cloud SQL for PostgreSQL (THREAD_DB_URL). On by default: the app is multi-user. Off (single-user demos only): the thread store is SQLite inside the container and resets with each restart or redeploy."
  default     = true
}

variable "db_tier" {
  type        = string
  description = "Cloud SQL machine tier (enable_cloud_sql only)."
  default     = "db-f1-micro"
}

variable "db_deletion_protection" {
  type        = bool
  description = "Block deleting the Cloud SQL instance. `make destroy` turns it off first."
  default     = true
}

variable "computer_service_account_id" {
  type        = string
  description = "Service account id users' computers run as. Empty: `<service_name>-computer`. Holds only roles/aiplatform.user, roles/logging.logWriter, pull access to the image repository, and IAP access to the app (for the daemon's MCP relay)."
  default     = ""

  validation {
    condition     = var.computer_service_account_id == "" || can(regex("^[a-z][-a-z0-9]{4,28}[a-z0-9]$", var.computer_service_account_id))
    error_message = "computer_service_account_id must be 6-30 lowercase letters, digits, or hyphens, or empty to use <service_name>-computer."
  }
}

variable "computer_image" {
  type        = string
  description = "The computer image every computer runs. Empty: <region>-docker.pkg.dev/<project>/<service_name>/computer:latest. Pin a tag (`make computer-image` prints it); bumping it makes a new instance template, and each computer is recreated from it on its next start, keeping its home disk."
  default     = ""
}

variable "computer_machine_type" {
  type        = string
  description = "Machine type of each user's computer."
  default     = "e2-standard-2"
}

variable "computer_zone" {
  type        = string
  description = "Zone users' computers and their disks are created in (COMPUTER_ZONE). Empty: <region>-a."
  default     = ""
}

variable "computer_subnet_cidr" {
  type        = string
  description = "Address range of the computers' subnet."
  default     = "10.20.0.0/20"
}

variable "app_subnet_cidr" {
  type        = string
  description = "Address range of the app's Direct VPC egress subnet (at least /26). The firewall lets it reach computers on port 8080."
  default     = "10.20.16.0/24"
}
