
output "vector_search_collection_id" {
  description = "Vector Search collection ID"
  value       = var.vs_collection_id
}

output "rag_gcs_bucket_name" {
  description = "RAG documents GCS bucket name"
  value       = google_storage_bucket.rag_docs.name
}

output "bq_dataset" {
  description = "BigQuery dataset for pipeline tables"
  value       = google_bigquery_dataset.rag_pipeline.dataset_id
}

output "bq_gcs_connection_id" {
  description = "BigQuery Cloud Resource connection ID"
  value       = google_bigquery_connection.rag_gcs.connection_id
}

output "pipeline_service_account_email" {
  description = "Vertex AI Pipeline service account email"
  value       = google_service_account.vertexai_pipeline_app_sa["dev"].email
}

output "pipeline_root" {
  description = "GCS root for Vertex AI Pipeline artifacts"
  value       = "gs://${google_storage_bucket.pipeline_root.name}/pipeline-root"
}

output "artifact_registry_repo" {
  description = "Artifact Registry Docker repository path"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.pipeline.repository_id}"
}
