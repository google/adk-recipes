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
  ambient_name    = "${local.prefix}-ambient"
  aiplatform_host = "https://${var.region}-aiplatform.googleapis.com"

  engine_api_path = "reasoningEngines/v1/${google_vertex_ai_reasoning_engine.aqa.id}/api"
}

resource "google_pubsub_topic" "ambient" {
  project = var.project_id
  name    = local.ambient_name
  labels  = local.common_labels
}

resource "google_pubsub_topic" "ambient_dead_letter" {
  project = var.project_id
  name    = "${local.ambient_name}-dead-letter"
  labels  = local.common_labels
}

resource "google_pubsub_subscription" "ambient_dead_letter" {
  project = var.project_id
  name    = "${local.ambient_name}-dead-letter"
  topic   = google_pubsub_topic.ambient_dead_letter.id
  labels  = local.common_labels

  # Nothing drains this.
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" }
}

resource "google_pubsub_subscription" "ambient_push" {
  depends_on = [time_sleep.ambient_caller]

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.ambient_dead_letter.id
    max_delivery_attempts = 5
  }

  # Measured 5s warm, so the 10s default leaves a cold start no headroom, and a
  # missed ack costs a second engine execution.
  ack_deadline_seconds = 60

  # Prevent subscription expiration during extended idle periods.
  expiration_policy { ttl = "" }

  project = var.project_id
  name    = "${local.ambient_name}-push"
  topic   = google_pubsub_topic.ambient.id
  labels  = local.common_labels

  push_config {
    push_endpoint = "${local.aiplatform_host}/${local.engine_api_path}/ambient/observed-update"

    # OIDC, because `push_config` supports nothing else.
    oidc_token {
      service_account_email = google_service_account.ambient_caller.email
      audience              = "${local.aiplatform_host}/"
    }

    # The container route parses an audit `LogEntry` directly.
    no_wrapper {
      write_metadata = false
    }
  }
}

# Calls the engine on a fixed period, whatever agent is investigated. The engine
# reads the agent's schedule from its attachment on each tick and investigates
# when that schedule fell due since the tick before, so the period bounds how
# late a scheduled investigation starts. A tick that is not due costs one
# request and no model spend.
resource "google_cloud_scheduler_job" "ambient_tick" {
  depends_on = [time_sleep.ambient_caller]

  project = var.project_id
  region  = var.region
  name    = "${local.ambient_name}-tick"

  # `TICK_PERIOD` in `ambient_quality_agent.tools.observed_agent_config.schedule`,
  # which a test pins.
  schedule  = "*/15 * * * *"
  time_zone = "Etc/UTC"

  paused = !var.scheduled_trigger_enabled

  http_target {
    http_method = "POST"
    uri         = "${local.aiplatform_host}/${local.engine_api_path}/ambient/trigger"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ type = "scheduled" }))

    # Agent Runtime custom endpoints reject Cloud Scheduler OIDC tokens with 401.
    oauth_token {
      service_account_email = google_service_account.ambient_caller.email
    }
  }

  retry_config {
    retry_count          = 5
    max_retry_duration   = "0s"
    min_backoff_duration = "5s"
    max_backoff_duration = "3600s"
    max_doublings        = 5
  }
}

# Maps state that addresses the job as `[0]` to the unindexed address, so the
# job is kept.
moved {
  from = google_cloud_scheduler_job.ambient_tick[0]
  to   = google_cloud_scheduler_job.ambient_tick
}

# Delays triggers until the new revision has traces. Tasks are named after the
# audit event, so a redelivered trigger collapses into one investigation.
resource "google_cloud_tasks_queue" "delay" {
  project  = var.project_id
  location = var.region
  name     = coalesce(var.task_queue_name, "${local.prefix}-delay")

  # One task per update, so logging every dispatch is cheap.
  stackdriver_logging_config {
    sampling_ratio = 1
  }
}
