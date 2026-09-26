# ==============================================================================
# Required
# ==============================================================================

variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "staging_bucket_name" {
  description = "GCS bucket name for Agent Engine staging artifacts. Must be globally unique."
  type        = string
}

variable "github_owner" {
  description = "GitHub repository owner (username or organisation)."
  type        = string
}

# ==============================================================================
# Optional — sensible defaults
# ==============================================================================

variable "region" {
  type    = string
  default = "us-central1"
}

variable "firestore_location" {
  type    = string
  default = "nam5"
}

variable "firestore_database_id" {
  type    = string
  default = "customer-support-db"
}

variable "ar_repo_name" {
  type    = string
  default = "customer-support"
}

variable "cloud_run_service_name" {
  type    = string
  default = "customer-support-app"
}

variable "github_repo" {
  type    = string
  default = "adk-multiagent-production-template"
}

variable "google_managed_sas_exist" {
  description = "Set to true after first Agent Engine deployment."
  type        = bool
  default     = false
}

variable "github_connected" {
  description = "Set to true after creating 2nd gen Cloud Build host connection."
  type        = bool
  default     = false
}

variable "agent_engine_resource_name" {
  description = "Full resource name of the deployed Agent Engine. Set after first deploy."
  type        = string
  default     = ""
}

variable "cloudbuild_connection_name" {
  description = "Name of the 2nd gen Cloud Build host connection."
  type        = string
  default     = ""
}

variable "cloudbuild_repo_name" {
  description = "Repository name as shown in Cloud Build 2nd gen (e.g. YOUR_GITHUB_USERNAME-adk-multiagent-production-template)."
  type        = string
  default     = ""
}

variable "model_armor_enabled" {
  type    = bool
  default = true
}

variable "model_armor_floor_mode" {
  type    = string
  default = "INSPECT_AND_BLOCK"
  validation {
    condition     = contains(["INSPECT_AND_BLOCK", "INSPECT_ONLY"], var.model_armor_floor_mode)
    error_message = "model_armor_floor_mode must be INSPECT_AND_BLOCK or INSPECT_ONLY."
  }
}

variable "tfstate_bucket_name" {
  description = "GCS bucket for remote Terraform state and tfvars. Defaults to {project_id}-tf-state if empty."
  type        = string
  default     = ""
}

variable "canary_traffic_percent" {
  description = "Percentage of traffic to send to the new revision on prod canary enable."
  type        = number
  default     = 10
}

variable "nightly_regression_threshold" {
  description = "Max allowed score drop vs GCS baseline before nightly eval fails (0.0–1.0)."
  type        = number
  default     = 0.05
}
