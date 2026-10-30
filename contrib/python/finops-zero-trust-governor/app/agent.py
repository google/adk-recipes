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

"""Multi-agent FinOps & Zero-Trust Governance Pipeline."""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps.app import App, ResumabilityConfig

from app.callbacks.mid_task_auth_guard import (
    enforce_token_and_retry_circuit_breaker,
    verify_token_not_revoked,
)
from app.tools.finops_remediation import (
    apply_model_routing_downgrade,
    update_cloud_run_scaling_limits,
)
from app.tools.telemetry_bq_tools import (
    inspect_agent_token_telemetry,
    query_bigquery_billing_anomalies,
)

_configured_model = os.environ.get("GEMINI_MODEL")
MODEL_NAME = _configured_model if _configured_model else "gemini-3.7-flash"

telemetry_investigator = LlmAgent(
    name="telemetry_investigator",
    model=MODEL_NAME,
    description=(
        "Read-only FinOps investigator that correlates BigQuery billing exports "
        "with Cloud Trace OpenTelemetry GenAI token spans."
    ),
    instruction="""You are the FinOps Telemetry Investigator.
1. Call `query_bigquery_billing_anomalies` (default lookback_hours=24 if not specified).
2. For the workload with the highest cost anomaly, call `inspect_agent_token_telemetry` using its `workload_label`.
3. Summarize the exact hourly USD spike, the bottleneck sub-agent name, average input/output tokens, cache hit rate, and Cloud Run scaling issue.""",
    tools=[query_bigquery_billing_anomalies, inspect_agent_token_telemetry],
    before_model_callback=enforce_token_and_retry_circuit_breaker,
    output_key="anomaly_diagnosis",
)

remediation_planner = LlmAgent(
    name="remediation_planner",
    model=MODEL_NAME,
    description=(
        "FinOps architecture planner that converts telemetry diagnoses into "
        "non-destructive model routing and scaling remediations."
    ),
    instruction="""You are the FinOps Remediation Planner.
Review the telemetry diagnosis from the previous step:
{anomaly_diagnosis}
Produce a concrete remediation action plan specifying:
1. Model Routing Downgrade: exact `workload_label`, `sub_agent_name`, `from_model` (e.g., gemini-2.5-pro), `to_model` (e.g., gemini-3.7-flash), and `enable_context_caching=True`.
2. Cloud Run Scaling Adjustment: exact `workload_label`, `new_min_instances=2`, and `new_max_instances=20`.
3. Total projected hourly USD savings.""",
    before_model_callback=enforce_token_and_retry_circuit_breaker,
    output_key="remediation_plan",
)

zero_trust_executor = LlmAgent(
    name="zero_trust_executor",
    model=MODEL_NAME,
    description=(
        "Zero-Trust Remediation Executor protected by a 5-gate governance harness "
        "(mid-task OAuth revocation, crash-resumable idempotency, mutex lock, and HITL gate)."
    ),
    instruction="""You are the Zero-Trust FinOps Executor.
Execute the remediation actions defined in the plan:
{remediation_plan}
1. Call `apply_model_routing_downgrade` and `update_cloud_run_scaling_limits` matching the plan parameters.
2. CRITICAL SECURITY RULE: If any tool returns `BLOCKED_BY_ZERO_TRUST_GUARDRAIL`, `BLOCKED_BY_CONCURRENCY_GUARDRAIL`, or `PAUSED_FOR_HITL_APPROVAL`, immediately STOP calling any further tools and output a clear Security & Governance Audit Alert explaining why execution was halted.
3. Otherwise, present a clean executive summary of the applied changes and total hourly USD saved.""",
    tools=[apply_model_routing_downgrade, update_cloud_run_scaling_limits],
    before_model_callback=enforce_token_and_retry_circuit_breaker,
    before_tool_callback=verify_token_not_revoked,
    output_key="execution_summary",
)

root_agent = SequentialAgent(
    name="finops_governor_pipeline",
    description=(
        "Deterministic 3-stage FinOps & Zero-Trust Governance pipeline that diagnoses "
        "Vertex AI token spend spikes and applies guarded, crash-resumable remediations."
    ),
    sub_agents=[
        telemetry_investigator,
        remediation_planner,
        zero_trust_executor,
    ],
)


def create_agent() -> SequentialAgent:
    """Return the root agent pipeline instance."""
    return root_agent


app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
