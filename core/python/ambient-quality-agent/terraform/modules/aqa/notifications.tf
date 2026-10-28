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


# The agent has no email credential, so the alert policy sends the email.

locals {
  engine_console_url = join("", [
    "https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/",
    var.region,
    "/agent-engines/",
    basename(google_vertex_ai_reasoning_engine.aqa.name),
    "?project=",
    var.project_id,
  ])
}

resource "google_monitoring_notification_channel" "investigation_finished" {
  for_each = toset(var.notification_emails)

  project = var.project_id
  # The console lists channels by display name only, so it carries the address.
  display_name = "${local.prefix} investigation finished (${each.value})"
  type         = "email"
  labels       = { email_address = each.value }
  user_labels  = local.common_labels
}

resource "google_monitoring_alert_policy" "investigation_finished" {
  count = length(var.notification_emails) > 0 ? 1 : 0

  project      = var.project_id
  display_name = "${local.prefix} investigation finished"
  combiner     = "OR"
  user_labels  = local.common_labels

  conditions {
    display_name = "An investigation needs attention"

    condition_matched_log {
      # Matches the decision; `_should_notify` makes it.
      #
      # One engine, because a project can hold several AQA deployments.
      #
      # Not `severity>=ERROR`. ADK raises a RuntimeError at
      # `cli/fast_api.py:919` after the HTTP 200 on every `stream_query` turn.
      #
      # Uses `basename` because the provider documents `name` as a full
      # resource path but returns the bare numeric id.
      filter = <<-EOT
        resource.type="aiplatform.googleapis.com/ReasoningEngine"
        resource.labels.reasoning_engine_id="${basename(google_vertex_ai_reasoning_engine.aqa.name)}"
        jsonPayload.event="aqa_investigation_finished"
        jsonPayload.should_notify=true
      EOT

      # `run_id` keys the incident, so the two places that log one ending
      # collapse into one email. Keep the rest short: a mail lists every label.
      label_extractors = {
        run_id              = "EXTRACT(jsonPayload.run_id)"
        status              = "EXTRACT(jsonPayload.status)"
        notify_reason       = "EXTRACT(jsonPayload.notify_reason)"
        insights_created    = "EXTRACT(jsonPayload.insights_created)"
        insights_recurring  = "EXTRACT(jsonPayload.insights_recurring)"
        traces_evaluated    = "EXTRACT(jsonPayload.traces_evaluated)"
        traces_eval_failed  = "EXTRACT(jsonPayload.traces_eval_failed)"
        traces_eval_errored = "EXTRACT(jsonPayload.traces_eval_errored)"
      }
    }
  }

  documentation {
    # The provider marks this optional. The API rejects a policy without it.
    mime_type = "text/markdown"

    subject = "AQuA: ${var.observed_deployment_name} investigation $${log.extracted_label.run_id}"
    content = <<-EOT
      $${log.extracted_label.notify_reason} in ${var.observed_deployment_name}.
      Run $${log.extracted_label.run_id} finished with status
      $${log.extracted_label.status}: $${log.extracted_label.insights_created} new,
      $${log.extracted_label.insights_recurring} already tracked,
      $${log.extracted_label.traces_eval_failed} of
      $${log.extracted_label.traces_evaluated} trace(s) failed, and
      $${log.extracted_label.traces_eval_errored} could not be judged.

      A trace that could not be judged is an evaluation fault on our side, not
      a defect in ${var.observed_deployment_name}: the sweep measured less than
      its numbers suggest, and no insight was recorded for it either way. Start
      with the run logs below rather than the defects.

      Paste everything below into a coding harness. If you are an agent
      reading this, it is your context.

      AQuA monitors the agent ${var.observed_deployment_name} in project
      ${var.project_id}, region ${var.region}.

      Defects recorded for this run:

          SELECT i.status, i.label, o.rubrics FROM
          `${var.project_id}.${google_bigquery_dataset.insights.dataset_id}.insight_occurrences` o
          JOIN `${var.project_id}.${google_bigquery_dataset.insights.dataset_id}.insights` i
          USING (insight_id) WHERE o.run_id = "$${log.extracted_label.run_id}"

      Run logs and failure details. Check these if the run failed, because a
      failed run writes no defects to BigQuery:

          gcloud logging read 'jsonPayload.run_id="$${log.extracted_label.run_id}"' --project=${var.project_id} --freshness=1d

      AQuA investigations:
      https://console.cloud.google.com/bigquery?project=${var.project_id}&ws=!1m5!1m4!4m3!1s${var.project_id}!2s${google_bigquery_dataset.insights.dataset_id}!3sinvestigations

      AQuA engine:
      ${local.engine_console_url}
    EOT
  }

  alert_strategy {
    # 300s is the lowest a log-match policy allows.
    notification_rate_limit { period = "300s" }

    # No email when it closes: the incident closes on a timer, not on a fix.
    notification_prompts = ["OPENED"]
    auto_close           = "1800s"
  }

  notification_channels = [
    for c in google_monitoring_notification_channel.investigation_finished : c.id
  ]
}
