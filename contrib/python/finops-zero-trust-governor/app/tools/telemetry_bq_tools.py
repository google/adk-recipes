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

"""Telemetry and billing connection tools querying BigQuery and Cloud Trace."""

from __future__ import annotations

import os
import re
from typing import Any

from google.adk.tools import ToolContext

BQ_TABLE_PATTERN = re.compile(
    r"^[a-zA-Z0-9_\-]+(?::[a-zA-Z0-9_\-]+)?\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+$"
)


def query_bigquery_billing_anomalies(
    lookback_hours: int = 24, tool_context: ToolContext | None = None
) -> dict[str, Any]:
    """Queries BigQuery GCP Billing Export to detect Vertex AI and Cloud Run spend spikes.

    Args:
        lookback_hours: Number of hours to analyze against the 7-day rolling baseline.
        tool_context: ADK ToolContext for storing compact telemetry state.
    """
    is_test_env = os.environ.get("INTEGRATION_TEST")
    is_test = is_test_env.upper() == "TRUE" if is_test_env else True
    threshold_env = os.environ.get("FINOPS_COST_SPIKE_THRESHOLD_USD")
    threshold = float(threshold_env) if threshold_env else 250.0

    if is_test:
        report = {
            "status": "anomaly_detected",
            "lookback_hours": lookback_hours,
            "threshold_usd": threshold,
            "artifact_uri": "artifact://finops/bq_billing_snapshot_latest.json",
            "anomalies": [
                {
                    "service": "Vertex AI",
                    "sku": "Gemini 2.5 Pro Input Tokens (us-central1)",
                    "baseline_hourly_usd": 18.50,
                    "current_hourly_usd": 412.80,
                    "delta_pct": 2131.3,
                    "workload_label": "customer-support-triage-agent",
                },
                {
                    "service": "Cloud Run",
                    "sku": "CPU Allocation Time (Always-On Min Instances)",
                    "baseline_hourly_usd": 12.00,
                    "current_hourly_usd": 94.00,
                    "delta_pct": 683.3,
                    "workload_label": "customer-support-triage-agent",
                },
            ],
        }
        if tool_context and hasattr(tool_context, "state"):
            tool_context.state["billing_anomalies_summary"] = {
                "top_workload": "customer-support-triage-agent",
                "total_hourly_spike_usd": 506.80,
                "artifact_uri": report["artifact_uri"],
            }
        return report

    try:
        from google.cloud import bigquery
    except ImportError:
        return {
            "status": "error",
            "message": "google-cloud-bigquery library is required for live telemetry queries.",
        }

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    table_id = os.environ.get("FINOPS_BQ_BILLING_TABLE")
    if not table_id:
        table_id = f"{project_id}.billing_export.gcp_billing_export_v1"

    if not BQ_TABLE_PATTERN.match(table_id):
        return {
            "status": "error",
            "message": f"Invalid BigQuery table identifier format: '{table_id}'",
        }
    client = bigquery.Client(project=project_id)
    query = (
        f"SELECT service.description AS service, sku.description AS sku, "  # noqa: S608
        f"ROUND(SUM(cost), 2) AS current_spend_usd FROM `{table_id}` "
        f"WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @hours HOUR) "
        f"AND service.description IN ('Vertex AI', 'Cloud Run') "
        f"GROUP BY 1, 2 HAVING current_spend_usd > @threshold "
        f"ORDER BY current_spend_usd DESC LIMIT 10"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("hours", "INT64", lookback_hours),
            bigquery.ScalarQueryParameter("threshold", "FLOAT64", threshold),
        ]
    )
    rows = [
        dict(row) for row in client.query(query, job_config=job_config).result()
    ]
    report = {
        "status": "anomaly_detected" if rows else "normal",
        "lookback_hours": lookback_hours,
        "anomalies": rows,
    }
    if tool_context and hasattr(tool_context, "state"):
        tool_context.state["billing_anomalies_summary"] = {
            "row_count": len(rows),
            "status": report["status"],
        }
    return report


def inspect_agent_token_telemetry(
    workload_label: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Inspects OpenTelemetry GenAI spans from Cloud Trace/BigQuery to pinpoint high-token sub-agents.

    Args:
        workload_label: The GCP workload or ADK app name to inspect.
        tool_context: ADK ToolContext for storing span diagnostics.
    """
    is_test_env = os.environ.get("INTEGRATION_TEST")
    is_test = is_test_env.upper() == "TRUE" if is_test_env else True
    if is_test:
        telemetry = {
            "workload_label": workload_label,
            "trace_sample_count": 1420,
            "bottleneck_sub_agent": "intent_classifier_node",
            "configured_model": "gemini-2.5-pro",
            "avg_input_tokens_per_turn": 48500,
            "avg_output_tokens_per_turn": 320,
            "context_cache_hit_rate": 0.0,
            "cloud_run_min_instances": 25,
            "root_cause": (
                "Sub-agent 'intent_classifier_node' is invoking gemini-2.5-pro with an un-cached "
                "45k-token system catalog on every single turn for simple classification, "
                "and Cloud Run min_instances is pinned to 25 during off-peak hours."
            ),
        }
        tool_context.state["trace_telemetry_summary"] = {
            "workload_label": workload_label,
            "bottleneck_sub_agent": "intent_classifier_node",
            "configured_model": "gemini-2.5-pro",
        }
        return telemetry

    return {
        "workload_label": workload_label,
        "status": "live_trace_queried",
        "note": "Configure Cloud Trace BigQuery sink for live OpenTelemetry GenAI span aggregation.",
    }
