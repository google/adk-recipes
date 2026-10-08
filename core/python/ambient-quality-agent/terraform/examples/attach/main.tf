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

# What starts an investigation of one attached agent: a scheduled tick, and an
# audit sink on the observed engine's updates. The tick runs in AQuA's project;
# the sink lives in the observed engine's project, where its audit entries are
# written, and publishes to AQuA's topic. `agents-cli aqua attach --apply`
# applies this root once per agent, with its own state, after the deployment
# root `../single-project` has created the AQuA instance these call. It takes
# that instance's handles from what its engine reports. Only for an instance
# provisioned with no observed agent: one that names its agent creates the same
# triggers itself, and its engine reports none.

locals {
  # The topic id is `projects/<project id>/topics/<name>`. Prefixing with the
  # topic's name, `<deployment>-aqua-ambient`, keeps two AQuA instances' triggers
  # for one agent apart in one project.
  project_id = split("/", var.ambient_topic)[1]
  name       = "${split("/", var.ambient_topic)[3]}-${var.observed_agent_name}"

  # An audit entry is written to the project of the resource it is about, and a
  # project sink only sees its own project's entries.
  observed_project_id = coalesce(var.observed_project_id, local.project_id)

  aiplatform_host = "https://${var.region}-aiplatform.googleapis.com"
  engine_api_path = "reasoningEngines/v1/${var.aqua_engine}/api"

  # Off without an engine id: the filter would then match every engine in the
  # project and buy an investigation per update.
  observe_updates = var.observed_engine_id != null
}

resource "google_logging_project_sink" "updates" {
  count = local.observe_updates ? 1 : 0

  project     = local.observed_project_id
  name        = "${local.name}-updates"
  destination = "pubsub.googleapis.com/${var.ambient_topic}"

  # Leaving this false breaks the sink: the next plan proposes true to false,
  # which `UpdateSink` rejects.
  unique_writer_identity = true

  filter = join(" AND ", [
    "protoPayload.serviceName=\"aiplatform.googleapis.com\"",

    # Substring match to capture all API versions (e.g. v1, v1beta1).
    "protoPayload.methodName:\"ReasoningEngineService.UpdateReasoningEngine\"",

    # Otherwise every engine update in the project matches, AQuA's own
    # redeploys included, and each match buys an investigation. Matched on the
    # id alone because the observed engine's region need not be AQuA's, and the
    # audit entry names the project by number.
    "protoPayload.resourceName=~\"/reasoningEngines/${var.observed_engine_id}$\"",

    # Filter to the completion entry, whose revision is live and has traces.
    "operation.last=\"true\"",

    "NOT protoPayload.status.code>0",
  ])
}

# Reading the identity off the sink is what orders this correctly, and what ties
# the grant to the sink's own presence: no sink, no identity to grant. The topic
# is AQuA's, whichever project the sink is in.
resource "google_pubsub_topic_iam_member" "sink_publisher" {
  count = local.observe_updates ? 1 : 0

  project = local.project_id
  topic   = var.ambient_topic
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.updates[0].writer_identity
}

resource "google_cloud_scheduler_job" "tick" {
  project  = local.project_id
  region   = var.region
  name     = "${local.name}-tick"
  schedule = var.investigation_schedule

  # Running this costs one investigation per window.
  paused = !var.scheduled_trigger_enabled

  http_target {
    http_method = "POST"
    uri         = "${local.aiplatform_host}/${local.engine_api_path}/ambient/trigger"
    headers     = { "Content-Type" = "application/json" }
    body        = base64encode(jsonencode({ type = "scheduled" }))

    # Agent Runtime custom endpoints reject Cloud Scheduler OIDC tokens with 401.
    oauth_token {
      service_account_email = var.ambient_caller_email
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
