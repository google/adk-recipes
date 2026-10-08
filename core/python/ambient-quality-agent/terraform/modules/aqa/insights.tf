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

locals {
  # A dataset id takes underscores where the prefix takes hyphens.
  insights_dataset = "${replace(local.prefix, "-", "_")}_insights"
}

resource "google_bigquery_dataset" "insights" {
  project     = var.project_id
  dataset_id  = local.insights_dataset
  location    = var.region
  description = "AQA's durable quality insights."
  labels      = local.common_labels

  delete_contents_on_destroy = var.force_destroy
}

# Table schemas and clustering configurations are defined in JSON files under schemas/.
locals {
  insights_table            = jsondecode(file("${path.module}/schemas/insights.json"))
  insight_occurrences_table = jsondecode(file("${path.module}/schemas/insight_occurrences.json"))
  insight_root_causes_table = jsondecode(file("${path.module}/schemas/insight_root_causes.json"))
}

resource "google_bigquery_table" "insights" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "insights"
  deletion_protection = !var.force_destroy

  # One row per distinct problem, updated in place as it recurs or resolves.
  clustering = local.insights_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.insights_table.schema)
}

resource "google_bigquery_table" "insight_occurrences" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "insight_occurrences"
  deletion_protection = !var.force_destroy

  # Append-only, one row per cluster per sweep.
  clustering = local.insight_occurrences_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.insight_occurrences_table.schema)
}

resource "google_bigquery_table" "insight_root_causes" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "insight_root_causes"
  deletion_protection = !var.force_destroy

  # Append-only table resolved newest-first per occurrence. Clustered on
  # insight_id because queries filter by insight rather than timestamp.
  # Permissions are inherited from the dataset-level IAM binding.
  clustering = local.insight_root_causes_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.insight_root_causes_table.schema)
}

resource "google_bigquery_dataset_iam_member" "insights_writer" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.insights.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = local.engine_member
}
