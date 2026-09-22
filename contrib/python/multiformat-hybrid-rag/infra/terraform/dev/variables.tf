# All variables are sourced from .env via TF_VAR_ exports in the Makefile.
# No tfvars file needed — .env is the single source of truth.

# =============================================================================
# GCP
# =============================================================================

variable "project_id" {
  type        = string
  description = "GCP project ID (from .env GOOGLE_CLOUD_PROJECT)"
}

variable "project_name" {
  type        = string
  description = "Project name used as a base for resource naming"
  default     = "multiformat-hybrid-rag"
}

variable "region" {
  type        = string
  description = "GCP region for resource deployment (from .env GOOGLE_CLOUD_LOCATION)"
  default     = "us-central1"
}

# =============================================================================
# GCS
# =============================================================================

variable "gcs_bucket" {
  type        = string
  description = "GCS bucket name for RAG documents (from .env GCS_BUCKET)"
}

variable "gcs_prefix" {
  type        = string
  description = "GCS prefix within the bucket (from .env GCS_PREFIX)"
  default     = "documents/"
}

# =============================================================================
# BigQuery
# =============================================================================

variable "bq_dataset" {
  type        = string
  description = "BQ dataset for pipeline tables (from .env BQ_DATASET)"
  default     = "rag_pipeline"
}

variable "bq_object_table" {
  type        = string
  description = "BQ Object Table name (from .env BQ_OBJECT_TABLE)"
  default     = "gcs_objects"
}

variable "bq_preprocessed_table" {
  type        = string
  description = "BQ preprocessed table name (from .env BQ_PREPROCESSED_TABLE)"
  default     = "preprocessed"
}

variable "bq_chunks_table" {
  type        = string
  description = "BQ chunks table name (from .env BQ_CHUNKS_TABLE)"
  default     = "chunks"
}

variable "bq_gcs_connection_id" {
  type        = string
  description = "BQ Cloud Resource connection ID for Object Table (from .env BQ_GCS_CONNECTION_ID)"
  default     = "rag-gcs-connection"
}

# =============================================================================
# Vector Search 2.0
# =============================================================================

variable "vs_collection_id" {
  type        = string
  description = "VS2 collection ID (from .env VS_COLLECTION_ID)"
  default     = "multiformat-hybrid-rag-collection"
}

variable "vs_documents_collection_id" {
  type        = string
  description = "VS2 documents collection ID (from .env VS_DOCUMENTS_COLLECTION_ID)"
  default     = "multiformat-hybrid-rag-documents"
}

# =============================================================================
# Gemini Models
# =============================================================================

variable "model_location" {
  type        = string
  description = "Endpoint serving the Gemini models. Separate from var.region: the gemini-3.x family is published only on the global endpoint and returns 404 from a regional one."
  default     = "global"
}

variable "markdown_converter_gemini_model" {
  type        = string
  description = "Gemini model for PDF/document to markdown conversion (from .env)"
  default     = "gemini-3.7-flash"
}

variable "relevance_gemini_model" {
  type        = string
  description = "Gemini model for document relevance classification (from .env)"
  default     = "gemini-3.5-flash-lite"
}

variable "contextual_chunking_gemini_model" {
  type        = string
  description = "Gemini model for contextual chunk enrichment (from .env)"
  default     = "gemini-3.5-flash-lite"
}

# =============================================================================
# Telemetry
# =============================================================================

variable "telemetry_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing telemetry data."
  default     = "labels.service_name=\"multiformat-hybrid-rag\" labels.type=\"agent_telemetry\""
}

variable "feedback_logs_filter" {
  type        = string
  description = "Log Sink filter for capturing feedback data."
  default     = "jsonPayload.log_type=\"feedback\" jsonPayload.service_name=\"multiformat-hybrid-rag\""
}

# =============================================================================
# IAM
# =============================================================================

variable "app_sa_roles" {
  description = "List of roles to assign to the application service account"
  type        = list(string)
  default = [
    "roles/aiplatform.user",
    "roles/vectorsearch.viewer",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/storage.admin",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser",
  ]
}

variable "pipelines_roles" {
  description = "List of roles to assign to the Vertex AI runner service account"
  type        = list(string)
  default = [
    "roles/storage.admin",
    "roles/run.invoker",
    "roles/aiplatform.user",
    "roles/discoveryengine.admin",
    "roles/logging.logWriter",
    "roles/artifactregistry.writer",
    "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser",
    "roles/bigquery.readSessionUser",
    "roles/bigquery.connectionAdmin",
    "roles/vectorsearch.admin",
    "roles/cloudfunctions.developer",
  ]
}
