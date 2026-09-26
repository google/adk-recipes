output "project_number" {
  description = "GCP project number."
  value       = data.google_project.project.number
}

output "staging_bucket" {
  description = "GCS staging bucket name for Agent Engine artifacts."
  value       = google_storage_bucket.staging.name
}

output "artifact_registry_url" {
  description = "Artifact Registry Docker image URL prefix. Append /<image>:<tag> when pushing."
  value       = local.ar_base_url
}

output "firestore_database_id" {
  description = "Firestore database ID."
  value       = google_firestore_database.main.name
}

output "agent_engine_sa" {
  description = "Vertex AI Agent Engine service account email."
  value       = local.agent_engine_sa
}

output "cloud_build_sa" {
  description = "Cloud Build service account email."
  value       = local.cloud_build_sa
}

output "cloud_run_sa" {
  description = "Cloud Run default compute service account email."
  value       = local.cloud_run_sa
}

output "nightly_trigger_id" {
  description = "Cloud Build nightly trigger ID (used by Cloud Scheduler and for manual runs). Empty until github_connected=true and environment=prod."
  value       = var.github_connected && var.environment == "prod" ? google_cloudbuild_trigger.nightly[0].trigger_id : ""
}

output "model_armor_enabled" {
  description = "Whether Model Armor floor settings are active."
  value       = var.model_armor_enabled
}

output "model_armor_template_name" {
  description = "Full resource name of the Model Armor template. Set MODEL_ARMOR_TEMPLATE_ID to this value in .env and Cloud Run env vars."
  value       = var.model_armor_enabled ? google_model_armor_template.customer_support_policy[0].name : ""
}

output "next_steps" {
  description = "Post-apply checklist."
  value = <<-EOT
    Infrastructure is ready (environment: ${var.environment}). Next steps:

    1. Seed Firestore with demo data:
         make seed-db

    2. (Optional) Add vector embeddings for RAG search:
         make add-embeddings

    3. Deploy the agent to Vertex AI Agent Engine:
         make deploy-agent-engine

    4. Build and deploy the backend to Cloud Run:
         make deploy-cloud-run

    5. After first Agent Engine deploy, set google_managed_sas_exist = true
       and agent_engine_resource_name in terraform/environments/${var.environment}/terraform.tfvars,
       then re-apply: terraform apply

    Artifact Registry URL: ${local.ar_base_url}
    Firestore database:    ${var.firestore_database_id}
    Staging bucket:        ${var.staging_bucket_name}
  EOT
}
