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

"""
Callback functions with state management for human-in-the-loop workflows.

Integrates with Firestore to log agent execution state, check prerequisites
before agent execution, and enable resume capability after human intervention.
"""

import json
from pathlib import Path

from google.adk.agents.callback_context import CallbackContext

from small_business_loan_agent.shared_libraries.constants import (
    AGENT_OUTPUT_KEY_MAP,
)
from small_business_loan_agent.shared_libraries.firestore_utils.state_service import (
    ProcessStateService,
)
from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)


def _get_agent_name(callback_context: CallbackContext) -> str:
    """Extract the agent name from the invocation context."""
    agent = callback_context._invocation_context.agent
    return agent.name if agent else "UnknownAgent"


def _build_cannot_proceed_error(
    agent_name: str, process_state: dict | None, request_id: str
) -> str:
    """Build a descriptive error message when an agent cannot proceed."""
    if not process_state:
        return f"Cannot proceed to {agent_name}: Process state not found for {request_id}"

    overall_status = process_state.get("overall_status")

    if overall_status == ProcessStateService.OVERALL_STATUS_PENDING_APPROVAL:
        issue_desc = ""
        for issue in process_state.get("issues", []):
            if not issue.get("resolved", False):
                issue_desc = issue.get("description", "")
                break
        error_msg = f"Cannot proceed to {agent_name}: Pending approval"
        if issue_desc:
            error_msg += f" - {issue_desc}"
        return error_msg

    return f"Cannot proceed to {agent_name}: Previous steps are not complete. Status: {overall_status}"


async def before_agent_callback_with_state_check(
    callback_context: CallbackContext,
) -> None:
    """Before-agent callback that checks if previous steps are complete."""
    enterprise_id = callback_context.state.get("loan_request_id")
    agent_name = _get_agent_name(callback_context)

    if not enterprise_id:
        raise ValueError(
            f"No loan_request_id found for {agent_name}. Please provide a request ID in the format SBL-YYYY-XXXXX."
        )

    state_service = ProcessStateService()

    if not state_service.can_proceed_to_step(enterprise_id, agent_name):
        process_state = state_service.get_process_status(enterprise_id)
        error_msg = _build_cannot_proceed_error(
            agent_name, process_state, enterprise_id
        )
        raise Exception(error_msg)

    process_state = state_service.get_process_status(enterprise_id)
    if process_state:
        callback_context.state["process_state"] = process_state
        logger.info(f"Loaded process state for {agent_name}")

    if (
        agent_name == "UnderwritingAgent"
        and "eligibility_rules" not in callback_context.state
    ):
        rules_path = (
            Path(__file__).parent.parent.parent
            / "sub_agents"
            / "underwriting"
            / "eligibility_rules.json"
        )
        with open(rules_path) as f:
            callback_context.state["eligibility_rules"] = json.dumps(
                json.load(f)
            )


def _check_for_issues(agent_name: str, output_data) -> tuple[bool, str, list]:
    """Check if agent output indicates issues requiring human review."""
    requires_review = False
    issue_description = ""
    missing_fields: list[str] = []

    if not isinstance(output_data, dict):
        return requires_review, issue_description, missing_fields

    if agent_name == "DocumentExtractionAgent":
        critical_fields = [
            "business_name",
            "owner_name",
            "loan_amount_requested",
            "annual_revenue",
        ]
        for field in critical_fields:
            value = output_data.get(field)
            if not value or (isinstance(value, str) and not value.strip()):
                missing_fields.append(field)

        if missing_fields:
            requires_review = True
            issue_description = f"Missing {len(missing_fields)} critical field(s): {', '.join(missing_fields)}"

    elif agent_name == "UnderwritingAgent":
        eligibility_status = output_data.get("eligibility_status")
        if eligibility_status == "INELIGIBLE":
            requires_review = True
            issue_description = (
                "Application failed eligibility check - requires manual review"
            )

    return requires_review, issue_description, missing_fields


def _persist_step_result(
    state_service: ProcessStateService,
    request_id: str,
    agent_name: str,
    output_data,
) -> None:
    """Persist the agent step result to Firestore."""
    requires_review, issue_description, missing_fields = _check_for_issues(
        agent_name, output_data
    )
    data = output_data if isinstance(output_data, dict) else None

    if requires_review:
        state_service.mark_step_for_review(
            request_id=request_id,
            step_name=agent_name,
            issue_description=issue_description,
            missing_fields=missing_fields,
            data=data,
        )
        logger.info(
            f"Marked {agent_name} for human review: {issue_description}"
        )
    else:
        state_service.update_step_status(
            request_id=request_id,
            step_name=agent_name,
            status=ProcessStateService.STATUS_COMPLETED,
            data=data,
        )
        logger.info(f"Marked {agent_name} as completed")

        if agent_name == ProcessStateService.STEP_LOAN_DECISION:
            state_service.mark_process_complete(request_id)
            logger.info(
                f"All steps completed - marked process {request_id} as complete"
            )


async def after_agent_callback_with_state_logging(
    callback_context: CallbackContext,
) -> None:
    """After-agent callback that logs agent completion to Firestore."""
    try:
        request_id = callback_context.state.get("loan_request_id")
        agent_name = _get_agent_name(callback_context)

        if not request_id:
            logger.warning(
                f"No loan_request_id found for {agent_name} - skipping state logging"
            )
            return

        output_key = AGENT_OUTPUT_KEY_MAP.get(agent_name)
        output_data = (
            callback_context.state.get(output_key) if output_key else None
        )

        if not output_data:
            logger.warning(
                f"No output data found for {agent_name} with key {output_key}"
            )

        state_service = ProcessStateService()
        _persist_step_result(state_service, request_id, agent_name, output_data)

    except Exception as e:
        logger.error(f"Error in after_agent_callback_with_state_logging: {e}")
