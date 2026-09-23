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

"""Orchestrator-level tools."""

from google.adk.tools.tool_context import ToolContext

from small_business_loan_agent.shared_libraries.constants import (
    AGENT_OUTPUT_KEY_MAP,
)
from small_business_loan_agent.shared_libraries.firestore_utils.state_service import (
    ProcessStateService,
)
from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)


def determine_process_action(process_state: dict) -> dict:
    """Determine the action to take for an existing process (pure function)."""
    overall_status = process_state.get("overall_status")
    current_step = process_state.get("current_step")
    request_id = process_state.get("loan_request_id", "")

    next_step, completed_steps = find_resume_point(process_state)

    if overall_status == ProcessStateService.OVERALL_STATUS_COMPLETED:
        action = "completed"
        message = f"Process {request_id} is already completed."
    elif overall_status == ProcessStateService.OVERALL_STATUS_PENDING_APPROVAL:
        action = "pending_approval"
        message = f"Process {request_id} is pending human approval."
    elif next_step:
        action = "resume"
        message = f"Process {request_id} can resume from {next_step}."
    else:
        action = "return_status"
        message = f"Process {request_id} status: {overall_status}"

    return {
        "status": "found",
        "action": action,
        "overall_status": overall_status,
        "current_step": current_step,
        "next_step_to_execute": next_step,
        "completed_steps": list(completed_steps.keys()),
        "message": message,
    }


def find_resume_point(process_state: dict) -> tuple[str | None, dict]:
    """Identify which steps are completed and which is next (pure function)."""
    steps = process_state.get("steps", {})
    completed_steps_data = {}
    next_step = None

    for step_name in ProcessStateService.ALL_STEPS:
        step_data = steps.get(step_name, {})
        step_status = step_data.get("status")

        if step_status in [
            ProcessStateService.STATUS_COMPLETED,
            ProcessStateService.STATUS_APPROVED,
        ]:
            step_output = step_data.get("data")
            if step_output:
                completed_steps_data[step_name] = step_output
        elif next_step is None:
            next_step = step_name

    return next_step, completed_steps_data


def check_process_status(tool_context: ToolContext) -> dict:
    """
    Check process status and initialize if needed.

    This is the FIRST tool that should ALWAYS be called by the orchestrator.
    """
    try:
        loan_request_id = tool_context.state.get("loan_request_id")

        if not loan_request_id:
            return {
                "status": "error",
                "message": "No loan_request_id found. Please provide a request ID.",
            }

        state_service = ProcessStateService()
        process_state = state_service.get_process_status(loan_request_id)

        # Process exists — determine action and load resume data
        if process_state:
            result = determine_process_action(process_state)

            _, completed_steps_data = find_resume_point(process_state)
            for step_name, step_output in completed_steps_data.items():
                output_key = AGENT_OUTPUT_KEY_MAP.get(step_name)
                if output_key:
                    tool_context.state[output_key] = step_output
                    logger.info(
                        f"Loaded {step_name} data into session state for resume"
                    )

            return result

        # Process doesn't exist — initialize new process
        session_id = tool_context._invocation_context.session.id
        state_service.create_process(loan_request_id, session_id)
        logger.info(f"Initialized new process for {loan_request_id}")

        return {
            "status": "initialized",
            "action": "proceed_to_analysis",
            "message": f"New process initialized for {loan_request_id}. Ready to begin processing.",
        }

    except Exception as e:
        logger.error(f"Error in check_process_status: {e}")
        return {"status": "error", "message": f"Error: {e!s}"}
