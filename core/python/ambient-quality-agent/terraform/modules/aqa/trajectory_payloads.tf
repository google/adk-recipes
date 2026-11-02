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

# The archived conversation behind a trajectory: a manifest row per
# conversation and a row per turn of it. Source telemetry ages out while an
# insight that recurs weekly lives indefinitely, so without these its evidence
# expires underneath it.
#
# Two grains, and the difference is the reason these are not columns on
# `trajectories`. That table records **one sweep's ingestion** and is keyed
# `(agent_name, run_id, trajectory_id)`; these record **the conversation** and
# are keyed `(agent_name, trajectory_id)`, so the bulky copy is stored once
# however many sweeps sampled it.
#
# Same dataset as the insight tables, so the engine's existing dataEditor grant
# covers them and no IAM change is needed.

# `schemas/` is the only definition of these two tables.
locals {
  trajectory_payloads_table      = jsondecode(file("${path.module}/schemas/trajectory_payloads.json"))
  trajectory_payload_turns_table = jsondecode(file("${path.module}/schemas/trajectory_payload_turns.json"))

  # The declaration carries the default so the module, the `bq` guide and the
  # schema test all read one number; the variable is the per-deployment
  # override. Both tables expire together -- a manifest whose turns have gone
  # is a conversation nobody can read.
  trajectory_payload_expiration_ms = 24 * 60 * 60 * 1000 * coalesce(
    var.trajectory_payload_retention_days,
    local.trajectory_payloads_table.partitioning.expiration_days,
  )
}

resource "google_bigquery_table" "trajectory_payloads" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "trajectory_payloads"
  deletion_protection = !var.force_destroy

  # Unlike `trajectories`, which is kept forever, these hold customer
  # conversation content and are ~50 KB apiece -- which is what a retention
  # policy is normally about, and why they are a separate table rather than a
  # column: BigQuery expires partitions, not columns.
  time_partitioning {
    type          = local.trajectory_payloads_table.partitioning.type
    field         = local.trajectory_payloads_table.partitioning.field
    expiration_ms = local.trajectory_payload_expiration_ms
  }

  clustering = local.trajectory_payloads_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.trajectory_payloads_table.schema)
}

resource "google_bigquery_table" "trajectory_payload_turns" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.insights.dataset_id
  table_id            = "trajectory_payload_turns"
  deletion_protection = !var.force_destroy

  # One turn per row rather than one conversation, so a long session cannot
  # exceed the load job's per-row limit and so a reader can size its batch to
  # its model's context instead of to whole conversations.
  time_partitioning {
    type          = local.trajectory_payload_turns_table.partitioning.type
    field         = local.trajectory_payload_turns_table.partitioning.field
    expiration_ms = local.trajectory_payload_expiration_ms
  }

  clustering = local.trajectory_payload_turns_table.clustering
  labels     = local.common_labels

  schema = jsonencode(local.trajectory_payload_turns_table.schema)
}
