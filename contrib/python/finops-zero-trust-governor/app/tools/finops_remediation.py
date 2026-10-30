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

"""Protected Remediation Tools: model routing and Cloud Run scaling."""

from __future__ import annotations

import time
from typing import Any

from google.adk.tools import ToolContext


def _record_execution(
    tool_context: ToolContext, idempotency_key: str, action: dict[str, Any]
) -> dict[str, Any]:
    """Helper to record applied remediations and cache idempotency state."""
    executed = tool_context.state.get("executed_remediations", [])
    executed.append(action)
    tool_context.state["executed_remediations"] = executed

    completed_map = tool_context.state.get("completed_idempotent_tools", {})
    completed_map[idempotency_key] = action
    if ":" in idempotency_key:
        parts = idempotency_key.split(":")
        base_key = f"{parts[0]}:{parts[1]}"
        completed_map[base_key] = action
    tool_context.state["completed_idempotent_tools"] = completed_map
    return action


def apply_model_routing_downgrade(
    workload_label: str,
    sub_agent_name: str,
    from_model: str,
    to_model: str,
    enable_context_caching: bool,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Updates Vertex AI model routing config for a high-spend sub-agent and enables context caching.

    Args:
        workload_label: Target ADK workload name.
        sub_agent_name: Specific sub-agent node to re-route.
        from_model: Current expensive model identifier.
        to_model: Target cost-optimized model identifier.
        enable_context_caching: Whether to enable Vertex AI explicit context caching.
        tool_context: ADK ToolContext.
    """
    action = {
        "status": "remediation_applied",
        "action_type": "MODEL_ROUTING_OPTIMIZATION",
        "workload_label": workload_label,
        "sub_agent_name": sub_agent_name,
        "from_model": from_model,
        "to_model": to_model,
        "enable_context_caching": enable_context_caching,
        "estimated_hourly_savings_usd": 365.40,
        "applied_at_epoch": int(time.time()),
    }
    idempotency_key = (
        f"apply_model_routing_downgrade:{workload_label}:{sub_agent_name}"
    )
    return _record_execution(tool_context, idempotency_key, action)


def update_cloud_run_scaling_limits(
    workload_label: str,
    new_min_instances: int,
    new_max_instances: int,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Adjusts Cloud Run min/max instance scaling bounds to eliminate idle compute burn.

    Args:
        workload_label: Target Cloud Run service name.
        new_min_instances: Updated minimum instance count.
        new_max_instances: Updated maximum instance count.
        tool_context: ADK ToolContext.
    """
    action = {
        "status": "remediation_applied",
        "action_type": "CLOUD_RUN_SCALING_ADJUSTMENT",
        "workload_label": workload_label,
        "new_min_instances": new_min_instances,
        "new_max_instances": new_max_instances,
        "estimated_hourly_savings_usd": 76.00,
        "applied_at_epoch": int(time.time()),
    }
    idempotency_key = f"update_cloud_run_scaling_limits:{workload_label}"
    return _record_execution(tool_context, idempotency_key, action)
