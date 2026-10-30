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

"""Unit tests for FinOps telemetry and protected remediation tools."""

from types import SimpleNamespace

from app.tools.finops_remediation import (
    apply_model_routing_downgrade,
    update_cloud_run_scaling_limits,
)
from app.tools.telemetry_bq_tools import (
    inspect_agent_token_telemetry,
    query_bigquery_billing_anomalies,
)


def test_query_bigquery_billing_anomalies_mock() -> None:
    """Verifies that billing anomaly query returns detected spend spikes in test mode."""
    context = SimpleNamespace(state={})
    report = query_bigquery_billing_anomalies(
        lookback_hours=24, tool_context=context
    )
    assert report["status"] == "anomaly_detected"
    assert len(report["anomalies"]) > 0
    assert "billing_anomalies_summary" in context.state
    assert (
        context.state["billing_anomalies_summary"]["total_hourly_spike_usd"]
        == 506.80
    )


def test_inspect_agent_token_telemetry_mock() -> None:
    """Verifies token span inspection returns subagent token diagnostics in test mode."""
    context = SimpleNamespace(state={})
    telemetry = inspect_agent_token_telemetry(
        workload_label="customer-support-triage-agent", tool_context=context
    )
    assert telemetry["workload_label"] == "customer-support-triage-agent"
    assert telemetry["bottleneck_sub_agent"] == "intent_classifier_node"
    assert telemetry["avg_input_tokens_per_turn"] == 48500
    assert "trace_telemetry_summary" in context.state


def test_apply_model_routing_downgrade() -> None:
    """Verifies model routing downgrade records remediation and idempotency map."""
    context = SimpleNamespace(state={})
    res = apply_model_routing_downgrade(
        workload_label="customer-support-triage-agent",
        sub_agent_name="intent_classifier_node",
        from_model="gemini-2.5-pro",
        to_model="gemini-3.7-flash",
        enable_context_caching=True,
        tool_context=context,
    )
    assert res["status"] == "remediation_applied"
    assert res["action_type"] == "MODEL_ROUTING_OPTIMIZATION"
    assert res["estimated_hourly_savings_usd"] == 365.40
    assert (
        "apply_model_routing_downgrade:customer-support-triage-agent"
        in context.state["completed_idempotent_tools"]
    )


def test_update_cloud_run_scaling_limits() -> None:
    """Verifies Cloud Run scaling update records remediation and idempotency map."""
    context = SimpleNamespace(state={})
    res = update_cloud_run_scaling_limits(
        workload_label="customer-support-triage-agent",
        new_min_instances=2,
        new_max_instances=20,
        tool_context=context,
    )
    assert res["status"] == "remediation_applied"
    assert res["action_type"] == "CLOUD_RUN_SCALING_ADJUSTMENT"
    assert res["new_min_instances"] == 2
    assert (
        "update_cloud_run_scaling_limits:customer-support-triage-agent"
        in context.state["completed_idempotent_tools"]
    )
