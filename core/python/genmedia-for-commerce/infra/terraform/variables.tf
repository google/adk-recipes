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
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run and Cloud Build"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "genmedia-for-commerce"
}

variable "media_retention_days" {
  description = "Days before objects under runs/ (saved inputs and results) and sessions/ (chat history, Studio state and jobs) in the media bucket are deleted"
  type        = number
  default     = 30
}

variable "deployer_email" {
  description = "Account running Terraform (make passes gcloud's active account). Empty: read from Terraform's credentials, which in Cloud Shell carry no email."
  type        = string
  default     = ""
}
