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

# BigQuery dataset for telemetry external tables
resource "google_bigquery_dataset" "telemetry_dataset" {
  project       = var.project_id
  dataset_id    = replace("${var.project_name}_telemetry", "-", "_")
  friendly_name = "${var.project_name} Telemetry"
  location      = var.region
  description   = "Dataset for GenAI telemetry data stored in GCS"
  depends_on    = [google_project_service.services]
}

# BigQuery connection for accessing GCS telemetry data
resource "google_bigquery_connection" "genai_telemetry_connection" {
  project       = var.project_id
  location      = var.region
  connection_id = "${var.project_name}-genai-telemetry"
  friendly_name = "${var.project_name} GenAI Telemetry Connection"

  cloud_resource {}

  depends_on = [google_project_service.services]
}

# Wait for the BigQuery connection service account to propagate in IAM
resource "time_sleep" "wait_for_bq_connection_sa" {
  create_duration = "10s"

  depends_on = [google_bigquery_connection.genai_telemetry_connection]
}

# Grant the BigQuery connection service account access to read from the logs bucket
resource "google_storage_bucket_iam_member" "telemetry_connection_access" {
  bucket = google_storage_bucket.logs_data_bucket.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_bigquery_connection.genai_telemetry_connection.cloud_resource[0].service_account_id}"

  depends_on = [time_sleep.wait_for_bq_connection_sa]
}

# ====================================================================
# Dedicated Cloud Logging Bucket for GenAI Telemetry
# ====================================================================

# Wait for the Logging API to fully propagate after enablement
resource "time_sleep" "wait_for_logging_api" {
  create_duration = "30s"

  depends_on = [google_project_service.services]
}

# Create a custom Cloud Logging bucket for GenAI telemetry logs with long-term retention
resource "google_logging_project_bucket_config" "genai_telemetry_bucket" {
  project          = var.project_id
  location         = var.region
  bucket_id        = "${var.project_name}-genai-telemetry"
  retention_days   = 3650 # 10 years retention (maximum allowed)
  enable_analytics = true # Required for linked datasets
  description      = "Dedicated Cloud Logging bucket for ${var.project_name} GenAI telemetry with 10 year retention"

  depends_on = [time_sleep.wait_for_logging_api]
}

# Log sink to route only GenAI telemetry logs to the dedicated bucket
# Filter by bucket name in the GCS path (which includes project_name) to isolate this agent's logs
resource "google_logging_project_sink" "genai_logs_to_bucket" {
  name        = "${var.project_name}-genai-logs"
  project     = var.project_id
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/${google_logging_project_bucket_config.genai_telemetry_bucket.bucket_id}"
  filter      = "log_name=\"projects/${var.project_id}/logs/gen_ai.client.inference.operation.details\" AND (labels.\"gen_ai.input.messages_ref\" =~ \".*${var.project_name}.*\" OR labels.\"gen_ai.output.messages_ref\" =~ \".*${var.project_name}.*\")"

  unique_writer_identity = true
  depends_on             = [google_logging_project_bucket_config.genai_telemetry_bucket]
}

# Create a linked dataset to the GenAI telemetry logs bucket for querying via BigQuery
resource "google_logging_linked_dataset" "genai_logs_linked_dataset" {
  link_id     = replace("${var.project_name}_genai_telemetry_logs", "-", "_")
  bucket      = google_logging_project_bucket_config.genai_telemetry_bucket.bucket_id
  description = "Linked dataset for ${var.project_name} GenAI telemetry Cloud Logging bucket"
  location    = var.region
  parent      = "projects/${var.project_id}"

  depends_on = [
    google_logging_project_bucket_config.genai_telemetry_bucket,
    google_logging_project_sink.genai_logs_to_bucket
  ]
}

# Wait for linked dataset to fully propagate
resource "time_sleep" "wait_for_linked_dataset" {
  create_duration = "10s"

  depends_on = [google_logging_linked_dataset.genai_logs_linked_dataset]
}

# ====================================================================
# Feedback Logs to Cloud Logging Bucket
# ====================================================================

# Log sink for user feedback logs - routes to the same Cloud Logging bucket
resource "google_logging_project_sink" "feedback_logs_to_bucket" {
  name        = "${var.project_name}-feedback"
  project     = var.project_id
  destination = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/${google_logging_project_bucket_config.genai_telemetry_bucket.bucket_id}"
  filter      = var.feedback_logs_filter

  unique_writer_identity = true
  depends_on             = [google_logging_project_bucket_config.genai_telemetry_bucket]
}

# ====================================================================
# Completions External Table (GCS-based)
# ====================================================================

# External table for completions data (messages/parts) stored in GCS
resource "google_bigquery_table" "completions_external_table" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.telemetry_dataset.dataset_id
  table_id            = "completions"
  deletion_protection = false

  external_data_configuration {
    autodetect            = false
    source_format         = "NEWLINE_DELIMITED_JSON"
    source_uris           = ["gs://${google_storage_bucket.logs_data_bucket.name}/completions/*"]
    connection_id         = google_bigquery_connection.genai_telemetry_connection.name
    ignore_unknown_values = true
    max_bad_records       = 1000
  }

  # Schema matching the ADK completions format
  schema = jsonencode([
    {
      name = "parts"
      type = "RECORD"
      mode = "REPEATED"
      fields = [
        { name = "type", type = "STRING", mode = "NULLABLE" },
        { name = "content", type = "STRING", mode = "NULLABLE" },
        { name = "mime_type", type = "STRING", mode = "NULLABLE" },
        { name = "uri", type = "STRING", mode = "NULLABLE" },
        { name = "data", type = "BYTES", mode = "NULLABLE" },
        { name = "id", type = "STRING", mode = "NULLABLE" },
        { name = "name", type = "STRING", mode = "NULLABLE" },
        { name = "arguments", type = "JSON", mode = "NULLABLE" },
        { name = "response", type = "JSON", mode = "NULLABLE" }
      ]
    },
    { name = "role", type = "STRING", mode = "NULLABLE" },
    { name = "index", type = "INTEGER", mode = "NULLABLE" }
  ])

  depends_on = [
    google_storage_bucket.logs_data_bucket,
    google_bigquery_connection.genai_telemetry_connection,
    google_storage_bucket_iam_member.telemetry_connection_access
  ]
}

# ====================================================================
# Completions View (Joins Logs with GCS Data)
# ====================================================================

# View that joins Cloud Logging data with GCS-stored completions data
resource "google_bigquery_table" "completions_view" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.telemetry_dataset.dataset_id
  table_id            = "completions_view"
  description         = "View of GenAI completion logs joined with the GCS prompt/response external table"
  deletion_protection = false

  view {
    query = templatefile("${path.module}/../sql/completions.sql", {
      project_id                 = var.project_id
      dataset_id                 = google_bigquery_dataset.telemetry_dataset.dataset_id
      completions_external_table = google_bigquery_table.completions_external_table.table_id
      logs_link_id               = google_logging_linked_dataset.genai_logs_linked_dataset.link_id
    })
    use_legacy_sql = false
  }

  depends_on = [
    google_logging_linked_dataset.genai_logs_linked_dataset,
    google_bigquery_table.completions_external_table,
    time_sleep.wait_for_linked_dataset
  ]
}
