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

# Both settings are stricter than a bucket's defaults. Uniform access removes
# per-object ACLs, leaving IAM as the only way in. `enforced` makes the ban on
# public access a property of the bucket rather than something inherited from an
# org policy the consumer of this module may not have.

resource "google_storage_bucket" "package" {
  project                     = var.project_id
  name                        = "${local.bucket_prefix}-package"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  labels = merge(local.common_labels, { purpose = "agent-package" })
}

# Stores observed agent source snapshots (one prefix per agent and deployment
# revision) cited during root-cause analysis.
#
# Expiration strands citations in older insights, causing browsing tools to
# report the revision as unreadable rather than returning incorrect data.
#
# Because GCS lifecycle rules expire objects by age rather than deployment
# status, an idle service exceeding the retention window loses its active
# revision snapshot until the next deploy. GCS cannot retain the N most recent
# revisions, so precise pruning belongs in the engine, which writes them.
resource "google_storage_bucket" "source" {
  project                     = var.project_id
  name                        = "${local.bucket_prefix}-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  labels = merge(local.common_labels, { purpose = "source-snapshot" })

  lifecycle_rule {
    condition {
      age = var.source_snapshot_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "jobs" {
  project                     = var.project_id
  name                        = "${local.bucket_prefix}-jobs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  labels = merge(local.common_labels, { purpose = "investigation-io" })

  # An idempotency marker is only read while its trigger can still repeat.
  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["aqa-idempotency-keys/"]
    }
    action {
      type = "Delete"
    }
  }

  # Chat transcripts (the A2A task store). Without a rule they accumulate for
  # the life of the deployment, and every one of them is downloaded whenever
  # the dashboard lists conversations.
  lifecycle_rule {
    condition {
      age            = 90
      matches_prefix = ["tasks/"]
    }
    action {
      type = "Delete"
    }
  }

  # Exported trace spans carry full conversation turns. Deleting them after the
  # retention window prevents indefinite storage growth and limits data exposure.
  lifecycle_rule {
    condition {
      age            = var.trace_retention_days
      matches_prefix = ["aqa-telemetry/"]
    }
    action {
      type = "Delete"
    }
  }
}

# Code-metric library. Published code runs in the agent's process, so write
# access equals deploy access. Read access is granted to the agent; write access
# is restricted to `var.metrics_writers`.
resource "google_storage_bucket" "metrics" {
  project                     = var.project_id
  name                        = "${local.bucket_prefix}-metrics"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.force_destroy

  labels = merge(local.common_labels, { purpose = "code-metric-library" })

  # Retain noncurrent metrics for 14 days for rollback. Unattended sweeps
  # discover bad publishes after the fact rather than during upload.
  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 14
      with_state                 = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }
}
