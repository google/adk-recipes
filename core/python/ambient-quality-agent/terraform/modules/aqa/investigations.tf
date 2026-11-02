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

# The investigation run registry: a run, and the progress events it emitted.
# Both share the dataset the insight tables live in
# (`google_bigquery_dataset.insights` -- the dataset the agent owns for its own
# tables, exported to the service as AQA_DATASET), so the engine's existing
# dataEditor grant covers them.

# `schemas/` is the only definition of these two tables.
locals {
  investigations_table       = jsondecode(file("${path.module}/schemas/investigations.json"))
  investigation_events_table = jsondecode(file("${path.module}/schemas/investigation_events.json"))
}

resource "google_bigquery_table" "investigations" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "investigations"
  deletion_protection = !var.force_destroy

  # Append-only: a run changes by gaining a newer snapshot of itself, and a read
  # takes the newest. Clustered on `run_id`, which every read filters on, and
  # not partitioned: no query here bounds itself by time, so a partition would
  # never be pruned (see the events table).
  clustering = local.investigations_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.investigations_table.schema)
}

resource "google_bigquery_table" "investigation_events" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "investigation_events"
  deletion_protection = !var.force_destroy

  # The many-to-one half of a run: one row per progress event, written by the
  # node that emitted it and never revisited. Clustered on `run_id` -- the only
  # thing a read of this table ever filters on -- and, like the table above,
  # not partitioned: a run's events are read by run, never by date, and at a
  # handful of rows per run a daily partition would hold far too little to earn
  # its metadata back.
  clustering = local.investigation_events_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.investigation_events_table.schema)
}
