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
Before-tool callback to prevent execution when process is halted.

Checks Firestore's overall_status before each tool execution to prevent
the orchestrator from calling subsequent agents when errors have occurred.
"""

from typing import Any

from google.adk.tools import BaseTool, ToolContext

from small_business_loan_agent.shared_libraries.firestore_utils.state_service import (
    ProcessStateService,
)
from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)


def determine_halt_action(
    tool_name: str, overall_status: str | None, issues: list
) -> dict | None:
    """Determine whether a tool execution should be halted (pure function)."""
    if overall_status == ProcessStateService.OVERALL_STATUS_PENDING_APPROVAL:
        issue_desc = "Unknown issue"
        for issue in issues:
            if not issue.get("resolved", False):
                issue_desc = issue.get("description", "Unknown issue")
                break
        return {
            "error": f"Cannot proceed to {tool_name}: Pending approval - {issue_desc}"
        }

    if overall_status == ProcessStateService.OVERALL_STATUS_FAILED:
        return {"error": f"Cannot proceed to {tool_name}: Process has failed"}

    if overall_status == ProcessStateService.OVERALL_STATUS_COMPLETED:
        return {
            "error": f"Cannot proceed to {tool_name}: Process already completed"
        }

    return None


async def before_tool_callback_check_process_status(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
) -> dict | None:
    """Before-tool callback — checks Firestore state before each agent tool execution."""
    try:
        if tool.name == "check_process_status":
            return None

        request_id = tool_context.state.get("loan_request_id")
        if not request_id:
            return None

        state_service = ProcessStateService()
        process_state = state_service.get_process_status(request_id)
        if not process_state:
            return None

        result = determine_halt_action(
            tool_name=tool.name,
            overall_status=process_state.get("overall_status"),
            issues=process_state.get("issues", []),
        )

        if result:
            logger.warning(
                f"Halting {tool.name} for {request_id}: {result['error']}"
            )

        return result

    except Exception as e:
        logger.error(f"Error in before_tool_callback_check_process_status: {e}")
        return None
