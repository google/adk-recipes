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

# One row per trajectory a sweep sampled, whatever became of it -- a whole
# session where the sweep judged one, a single turn where it judged that.
# Shares the dataset the insight tables live in
# (`google_bigquery_dataset.insights`, exported to the service as AQA_DATASET),
# so the engine's existing dataEditor grant covers it and no IAM change is
# needed.

# `schemas/` is the only definition of this table.
locals {
  trajectories_table = jsondecode(file("${path.module}/schemas/trajectories.json"))
}

resource "google_bigquery_table" "trajectories" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "trajectories"
  deletion_protection = !var.force_destroy

  # The first partitioned table in the dataset, and the only one whose reads
  # bound themselves by time: the per-day outcome chart scans a window, so a
  # daily partition is pruned on every read of it. Clustered on the two columns
  # every read also filters on.
  time_partitioning {
    type  = local.trajectories_table.partitioning.type
    field = local.trajectories_table.partitioning.field
  }

  # No partition expiration. A row is ~200 bytes and a sweep writes at most
  # `2 x data_evaluation_cap` of them, so a busy agent produces single-digit
  # megabytes a month -- and this is the history the chart reads, which is the
  # one thing that must outlive the telemetry it was derived from.
  clustering = local.trajectories_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.trajectories_table.schema)
}
