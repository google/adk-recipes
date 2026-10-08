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
  prefix        = "${var.observed_deployment_name}-aqua"
  bucket_prefix = "${var.project_id}-${local.prefix}"

  # What AQA matches `gen_ai.agent.name` against. Derived only as a fallback:
  # `agents-cli` passes the root agent name it actually scaffolded, which need
  # not be a plain transliteration of the deployment's name.
  observed_agent_name = coalesce(
    var.observed_agent_name,
    replace(var.observed_deployment_name, "-", "_"),
  )

  telemetry_dataset = coalesce(
    var.telemetry_dataset,
    "${replace(var.observed_deployment_name, "-", "_")}_telemetry",
  )

  telemetry_table = coalesce(
    var.telemetry_table,
    "aiplatform_googleapis_com_reasoning_engine_stdout",
  )

  telemetry_location = coalesce(var.telemetry_location, var.region)

  telemetry_payload_bucket = coalesce(
    var.telemetry_payload_bucket,
    "${var.project_id}-${var.observed_deployment_name}-logs",
  )

  # `observed_deployment` is what tells two deployments in one project apart.
  # The deployment, not the agent: a renamed root agent must not look like a
  # different set of resources.
  common_labels = {
    component           = "aqa"
    observed_deployment = var.observed_deployment_name
  }
}
