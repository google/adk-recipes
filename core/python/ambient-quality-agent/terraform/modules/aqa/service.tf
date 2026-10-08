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
  # Agent Runtime will not create an engine with no source, and `agents-cli
  # deploy` replaces this. It is a gzipped tar of a hello-world Dockerfile:
  # `tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='UTC 2020-01-01' -czf - Dockerfile | base64 -w0`.
  dummy_source_b64 = "H4sIAAAAAAAAA+3STWrDMBAFYK99ioFsG1uOgw3ZFdruSktPECFNbRFFCvpJ6e2rGEqh+6RdvG8zoCc0w6AHrw4c3o3l6mpEMWzFUovfVfRdV3X9IDbjILbL+Thu+orE9Ub6kWOSgegWrf6jFb1aqXj2VnMgc5QTN3Q/sUv06CbjmD6MteR8IhVYJibpiL+TNJeEos9B8V1JdL2iNJtIR6+zZZplvFxQXjMlT5M5M5m0o728dIhrZQ1pPln/uadQapkkLg809dPbyzPluNbLB21Oh6nRfG6V9VmH7FrlXZJlitDObK2v/3qRAAAAAAAAAAAAAAAAAAAAAAAAN/YFegdMYwAoAAA="

  # What the agent cannot work out for itself. The observed agent's part is
  # left out when `configure_observed_agent` is false, and the engine then reads
  # it from the agent attached with `agents-cli aqua attach`.
  engine_env = merge(
    local.aqua_env,
    var.configure_observed_agent ? local.observed_agent_env : local.attach_env,
  )

  aqua_env = {
    AQA_JOBS_GCS_BUCKET    = google_storage_bucket.jobs.name
    AQA_SOURCE_GCS_BUCKET  = google_storage_bucket.source.name
    AQA_METRICS_GCS_BUCKET = google_storage_bucket.metrics.name
    AQA_DATASET            = google_bigquery_dataset.insights.dataset_id
    AQA_DATASET_LOCATION   = google_bigquery_dataset.insights.location

    # `agents-cli` sets GOOGLE_CLOUD_LOCATION=global, and a global endpoint
    # answers 501.
    AQA_ENGINE_LOCATION = var.region

    # The agent requires both of these, or neither.
    AQA_DELAY_TASK_QUEUE  = google_cloud_tasks_queue.delay.id
    AQA_AMBIENT_CALLER_SA = google_service_account.ambient_caller.email
  }

  # What `agents-cli aqua attach --apply` needs to wire an attached agent's
  # triggers to this engine, which reports it from the attach route along with
  # the caller account, so that attaching takes no more than the engine's
  # resource name. Left out when the deployment names its observed agent and
  # runs that agent's triggers itself.
  attach_env = {
    AQA_AMBIENT_TOPIC = google_pubsub_topic.ambient.id
  }

  observed_agent_env = {
    # The ADK agent name rather than `observed_deployment_name`: this is matched
    # against the `gen_ai.agent.name` label on each telemetry row, and the
    # infrastructure identity beside it is hyphenated where that label is not.
    AQA_OBSERVED_AGENT_NAME = local.observed_agent_name

    # Without these, the agent defaults to the ADK `agent_analytics` dataset,
    # which `agents-cli` projects do not create. `cloud_ops` reads a linked
    # dataset, requiring the `roles/logging.viewAccessor` grant in `iam.tf`.
    TELEMETRY_INGESTION_SOURCE = var.telemetry_ingestion_source
    AQA_TELEMETRY_DATASET      = local.telemetry_dataset
    AQA_TELEMETRY_LOCATION     = local.telemetry_location
    AQA_TELEMETRY_TABLE        = local.telemetry_table

    # Wait duration for new traces before triggering.
    AQA_AGENT_REVISION_TRIGGER_DELAY_SECONDS = tostring(var.agent_revision_trigger_delay_seconds)
  }
}

# Tracks the environment itself. Ignoring `spec` below means Terraform can never
# update a running engine's environment, so a change that cannot be applied has
# to replace the engine rather than be dropped in silence.
resource "terraform_data" "engine_identity" {
  input = local.engine_env
}

# Terraform owns the engine's configuration, `agents-cli deploy` owns its code.
# That is what lets the environment be written as references to the resources
# beside it.
#
# The deploy finds the engine to update by `display_name`, so it has to be given
# the same one, with `--service-name`.
resource "google_vertex_ai_reasoning_engine" "aqa" {
  # Cannot start before the service agent may mint a token for its account.
  depends_on = [time_sleep.engine_token_creator]

  project      = var.project_id
  region       = var.region
  display_name = local.prefix
  description  = "Ambient Quality Agent watching ${var.observed_deployment_name}. Code is deployed by agents-cli."

  # The platform refuses to delete an engine that still has children, and one
  # turn leaves a session behind. The cascade can also take memories and sandbox
  # environments; AQA creates neither.
  deletion_policy = "FORCE"

  spec {
    agent_framework = "google-adk"
    service_account = google_service_account.engine.email

    deployment_spec {
      resource_limits = {
        cpu    = var.engine_cpu
        memory = var.engine_memory
      }

      dynamic "env" {
        for_each = local.engine_env

        content {
          name  = env.key
          value = env.value
        }
      }
    }

    source_code_spec {
      inline_source {
        source_archive = local.dummy_source_b64
      }
      image_spec {}
    }
  }

  lifecycle {
    # The whole attribute, not its parts. The provider sets `updateMask=spec`
    # on any change inside it and sends the block back without the deployed
    # code, so a narrower ignore still lets one apply overwrite the deploy.
    # `agents-cli deploy` guarantees such a change by writing `class_methods`,
    # which has no counterpart here.
    ignore_changes = [spec]

    # Any change to that environment, a rename included, replaces the engine.
    # The code is redeployed afterwards either way, since Terraform never held
    # it.
    replace_triggered_by = [terraform_data.engine_identity]
  }
}
