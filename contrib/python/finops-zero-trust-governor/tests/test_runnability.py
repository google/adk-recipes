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

"""Zero-Credential Unit & Governance Gate Tests.

Verifies:
  1. root_agent, App, and SequentialAgent pipeline definition.
  2. Gate 1: Token & Retry Circuit Breaker (before_model_callback).
  3. Gate 2: Mid-Task OAuth/IAM Revocation Check (Issue #2769).
  4. Gate 3: Crash-Resumable Tool Idempotency Guard (ResumabilityConfig replay).
  5. Gate 4: Concurrent Workload Mutex Lock.
  6. Gate 5: Blast-Radius Human-in-the-Loop (HITL) Threshold Gate.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.apps import App
from google.adk.models.llm_response import LlmResponse

import app
import app.agent
from app.agent import root_agent, zero_trust_executor
from app.callbacks.mid_task_auth_guard import (
    enforce_token_and_retry_circuit_breaker,
    verify_token_not_revoked,
)

os.environ["INTEGRATION_TEST"] = "TRUE"


@pytest.fixture(autouse=True)
def setup_test_env():
    """Ensure zero-credential test mode and deterministic thresholds."""
    os.environ["INTEGRATION_TEST"] = "TRUE"
    os.environ["FINOPS_MAX_LLM_TURNS_PER_RUN"] = "3"
    os.environ["ZERO_TRUST_ENFORCE_REVOCATION"] = "TRUE"
    yield


def test_root_agent_structure() -> None:
    """Verifies root_agent loads without GCP credentials and wires all 3 sub-agents."""
    assert root_agent is not None
    assert root_agent.name == "finops_governor_pipeline"

    sub_names = [agent.name for agent in root_agent.sub_agents]
    assert sub_names == [
        "telemetry_investigator",
        "remediation_planner",
        "zero_trust_executor",
    ]
    assert zero_trust_executor.before_tool_callback is not None

    # Verify App and ResumabilityConfig
    assert app.agent.app is not None
    assert isinstance(app.agent.app, App)
    assert app.agent.app.resumability_config is not None
    assert app.agent.app.resumability_config.is_resumable is True


def test_mid_task_auth_guard_allows_valid_session() -> None:
    """Verifies before_tool_callback permits execution when token is active."""
    mock_tool = MagicMock()
    mock_tool.name = "apply_model_routing_downgrade"

    mock_context = MagicMock()
    mock_context.state = {"auth_token_revoked": False}

    result = verify_token_not_revoked(
        tool=mock_tool,
        args={"workload_label": "customer-support-triage-agent"},
        tool_context=mock_context,
    )

    assert result is None
    assert (
        mock_context.state["security_audit_log"][-1]["event_type"]
        == "MID_TASK_AUTH_VERIFIED"
    )


def test_mid_task_auth_guard_blocks_revoked_session() -> None:
    """Verifies Issue #2769: mid-task token revocation blocks write tool execution."""
    mock_tool = MagicMock()
    mock_tool.name = "apply_model_routing_downgrade"

    mock_context = MagicMock()
    # Simulate token revoked while telemetry_investigator / remediation_planner were running
    mock_context.state = {"auth_token_revoked": True}

    result = verify_token_not_revoked(
        tool=mock_tool,
        args={"workload_label": "customer-support-triage-agent"},
        tool_context=mock_context,
    )

    assert result is not None
    assert result["status"] == "BLOCKED_BY_ZERO_TRUST_GUARDRAIL"
    assert result["error_code"] == "AUTH_TOKEN_REVOKED_MID_TASK"
    assert (
        mock_context.state["security_audit_log"][-1]["event_type"]
        == "MID_TASK_AUTH_REVOCATION_BLOCK"
    )


def test_gate_1_token_retry_circuit_breaker() -> None:
    """Test Gate 1 halts when invocation turns exceed FINOPS_MAX_LLM_TURNS_PER_RUN."""
    state: dict[str, Any] = {"invocation_llm_turns": 0}
    context = SimpleNamespace(state=state)

    # First 3 turns should pass (limit is 3)
    res1 = enforce_token_and_retry_circuit_breaker(context, MagicMock())
    assert res1 is None
    assert state["invocation_llm_turns"] == 1

    res2 = enforce_token_and_retry_circuit_breaker(context, MagicMock())
    assert res2 is None
    assert state["invocation_llm_turns"] == 2

    res3 = enforce_token_and_retry_circuit_breaker(context, MagicMock())
    assert res3 is None
    assert state["invocation_llm_turns"] == 3

    # Turn 4 exceeds limit of 3 -> trips circuit breaker
    res4 = enforce_token_and_retry_circuit_breaker(context, MagicMock())
    assert res4 is not None
    assert isinstance(res4, LlmResponse)
    assert "CIRCUIT_BREAKER_TRIPPED" in res4.content.parts[0].text
    assert len(state["security_audit_log"]) == 1
    assert (
        state["security_audit_log"][0]["event_type"]
        == "CIRCUIT_BREAKER_TRIPPED"
    )


def test_gate_3_crash_resumable_tool_idempotency() -> None:
    """Test Gate 3 skips duplicate mutation during crash-recovery replay."""
    tool = SimpleNamespace(name="apply_model_routing_downgrade")
    workload_label = "customer-support-triage-agent"
    args = {"workload_label": workload_label}

    cached_action = {
        "status": "remediation_applied",
        "action_type": "MODEL_ROUTING_OPTIMIZATION",
        "estimated_hourly_savings_usd": 365.40,
    }
    state = {
        "completed_idempotent_tools": {
            f"{tool.name}:{workload_label}": cached_action,
        },
        "security_audit_log": [],
    }
    context = SimpleNamespace(state=state, invocation_id="inv-replay")

    res = verify_token_not_revoked(tool, args, context)
    assert res is not None
    assert res["resumed_from_checkpoint"] is True
    assert res["status"] == "remediation_applied"
    assert res["estimated_hourly_savings_usd"] == 365.40


def test_gate_4_concurrent_workload_mutex() -> None:
    """Test Gate 4 prevents concurrent race conditions on the same GCP service."""
    tool = SimpleNamespace(name="apply_model_routing_downgrade")
    workload_label = "shared-production-pipeline"
    args = {"workload_label": workload_label}

    state = {
        "active_workload_locks": {
            workload_label: "concurrent-invocation-alpha",
        },
        "security_audit_log": [],
    }
    context = SimpleNamespace(
        state=state, invocation_id="current-invocation-beta"
    )

    res = verify_token_not_revoked(tool, args, context)
    assert res is not None
    assert res["status"] == "BLOCKED_BY_CONCURRENCY_GUARDRAIL"
    assert res["error_code"] == "CONCURRENT_MUTATION_LOCK_CONFLICT"


def test_gate_5_blast_radius_hitl_escalation() -> None:
    """Test Gate 5 blocks scaling to 0 min_instances unless human_hitl_approved=True."""
    tool = SimpleNamespace(name="update_cloud_run_scaling_limits")
    args_zero = {
        "workload_label": "customer-support-triage-agent",
        "new_min_instances": 0,
    }

    # Case A: Without HITL approval -> Paused for HITL
    state_unapproved = {"human_hitl_approved": False, "security_audit_log": []}
    context_unapproved = SimpleNamespace(
        state=state_unapproved, invocation_id="inv-hitl-1"
    )

    res_blocked = verify_token_not_revoked(tool, args_zero, context_unapproved)
    assert res_blocked is not None
    assert res_blocked["status"] == "PAUSED_FOR_HITL_APPROVAL"
    assert res_blocked["error_code"] == "BLAST_RADIUS_EXCEEDS_AUTONOMOUS_LIMIT"

    # Case B: With human_hitl_approved=True -> Allowed
    state_approved = {"human_hitl_approved": True, "security_audit_log": []}
    context_approved = SimpleNamespace(
        state=state_approved, invocation_id="inv-hitl-2"
    )

    res_allowed = verify_token_not_revoked(tool, args_zero, context_approved)
    assert res_allowed is None  # Allowed
