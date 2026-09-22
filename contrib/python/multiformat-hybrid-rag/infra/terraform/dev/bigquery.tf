# BigQuery resources for the RAG pipeline.
#
# Creates the dataset, Cloud Resource connection (for Object Table access
# to GCS), the Object Table, and the preprocessed/chunks tables that
# track pipeline state.

# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
resource "google_bigquery_dataset" "rag_pipeline" {
  dataset_id = var.bq_dataset
  project    = var.project_id
  location   = var.region

  description = "RAG pipeline metadata: preprocessing state, chunks, and GCS object tracking"

  depends_on = [resource.google_project_service.services]
}

# ---------------------------------------------------------------------------
# Cloud Resource Connection
# ---------------------------------------------------------------------------
# Allows the Object Table to read GCS bucket metadata. GCP auto-provisions
# a service account for the connection; we grant it objectViewer below.
resource "google_bigquery_connection" "rag_gcs" {
  connection_id = var.bq_gcs_connection_id
  project       = var.project_id
  location      = var.region

  cloud_resource {}

  depends_on = [resource.google_project_service.services]
}

# Grant the connection's auto-provisioned SA read access to the RAG bucket
resource "google_storage_bucket_iam_member" "bq_connection_gcs_reader" {
  bucket = google_storage_bucket.rag_docs.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_bigquery_connection.rag_gcs.cloud_resource[0].service_account_id}"
}

# ---------------------------------------------------------------------------
# Object Table (external — mirrors GCS bucket metadata)
# ---------------------------------------------------------------------------
# External table backed by GCS object metadata (not file content). Exposes
# columns: uri, md5_hash, content_type, size, updated, generation. The
# pipeline uses it to cheaply detect new, changed, and deleted files
# without scanning file contents.
resource "google_bigquery_table" "gcs_objects" {
  dataset_id          = google_bigquery_dataset.rag_pipeline.dataset_id
  table_id            = var.bq_object_table
  project             = var.project_id
  deletion_protection = false

  external_data_configuration {
    connection_id   = google_bigquery_connection.rag_gcs.name
    autodetect      = false
    object_metadata = "SIMPLE"
    source_uris     = ["gs://${google_storage_bucket.rag_docs.name}/${var.gcs_prefix}*"]
  }

  depends_on = [
    google_bigquery_dataset.rag_pipeline,
    google_storage_bucket_iam_member.bq_connection_gcs_reader,
  ]
}

# ---------------------------------------------------------------------------
# Preprocessed Table
# ---------------------------------------------------------------------------
# Stores extracted text from source documents. Each row = one file in GCS.
# file_id (MD5 of gcs_uri) is the primary key. content_hash tracks the
# GCS md5_hash to detect content changes without re-reading the file.
resource "google_bigquery_table" "preprocessed" {
  dataset_id          = google_bigquery_dataset.rag_pipeline.dataset_id
  table_id            = var.bq_preprocessed_table
  project             = var.project_id
  deletion_protection = false

  description = "Extracted text from source documents in GCS"

  schema = jsonencode([
    { name = "file_id", type = "STRING", mode = "REQUIRED", description = "MD5 hex digest of gcs_uri" },
    { name = "gcs_uri", type = "STRING", mode = "REQUIRED", description = "Full GCS URI (gs://bucket/prefix/file)" },
    { name = "content_hash", type = "STRING", mode = "REQUIRED", description = "GCS md5_hash for change detection" },
    { name = "content", type = "STRING", mode = "REQUIRED", description = "Extracted text content" },
    { name = "content_length", type = "INT64", mode = "NULLABLE", description = "LENGTH(content) — avoids full-column scans for dedup queries" },
    { name = "file_name", type = "STRING", mode = "NULLABLE", description = "Original filename from URI" },
    { name = "file_type", type = "STRING", mode = "NULLABLE", description = "File extension (md, pdf, etc.)" },
    { name = "extracted_at", type = "TIMESTAMP", mode = "REQUIRED", description = "Extraction timestamp — compared to chunked_at to detect stale chunks" },
    { name = "relevant", type = "BOOL", mode = "REQUIRED", defaultValueExpression = "TRUE", description = "Whether the document is relevant to the knowledge base (from LLM classifier)" },
    { name = "error", type = "STRING", mode = "NULLABLE", description = "Non-null when extraction or classification raised — message for debugging. NULL = success." },
  ])

  depends_on = [google_bigquery_dataset.rag_pipeline]
}

# ---------------------------------------------------------------------------
# Chunks Table
# ---------------------------------------------------------------------------
# Stores chunked text ready for Vector Search indexing. Each row = one
# chunk. chunk_id = "{file_id}__{chunk_index}" is the primary key.
# When a file is re-chunked, all its old rows are deleted and replaced.
resource "google_bigquery_table" "chunks" {
  dataset_id          = google_bigquery_dataset.rag_pipeline.dataset_id
  table_id            = var.bq_chunks_table
  project             = var.project_id
  deletion_protection = false

  description = "Chunked text ready for Vector Search indexing"

  schema = jsonencode([
    { name = "chunk_id", type = "STRING", mode = "REQUIRED", description = "Deterministic ID: {file_id}__{chunk_index}" },
    { name = "file_id", type = "STRING", mode = "REQUIRED", description = "FK to preprocessed table" },
    { name = "gcs_uri", type = "STRING", mode = "REQUIRED", description = "Source file GCS URI" },
    { name = "chunk_index", type = "INT64", mode = "REQUIRED", description = "Zero-based position within the file" },
    { name = "chunk_text", type = "STRING", mode = "REQUIRED", description = "The chunk text content" },
    { name = "context", type = "STRING", mode = "NULLABLE", description = "LLM-generated contextual summary for the chunk (Anthropic contextual retrieval technique)" },
    { name = "chunked_at", type = "TIMESTAMP", mode = "REQUIRED", description = "When this chunk was created" },
    { name = "indexed_at", type = "TIMESTAMP", mode = "NULLABLE", description = "When VS2 indexing was confirmed for this chunk — NULL until VS2 returns success. Used by the rechunk-detection query to avoid silent drift on partial VS2 failures." },
  ])

  depends_on = [google_bigquery_dataset.rag_pipeline]
}
