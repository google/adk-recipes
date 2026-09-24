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
LLM-as-Judge Gate for the Small Business Loan Agent.

Validates agent responses before they are shown to users by checking:
1. Trajectory correctness — Did the agent follow the expected tool sequence?
2. Grounding — Are all values traceable to agent outputs?
3. Response quality — Is the response complete and actionable?
"""

import json
import os
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.genai import Client
from google.genai import types as genai_types
from google.genai.types import GenerateContentConfig

from small_business_loan_agent.callbacks.after_agent_callback.models import (
    JudgeVerdict,
)
from small_business_loan_agent.callbacks.after_agent_callback.prompt import (
    JUDGE_PROMPT,
)
from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)

JUDGE_MODEL = os.getenv("JUDGE_MODEL")
if not JUDGE_MODEL:
    JUDGE_MODEL = os.getenv("MODEL_NAME")

AGENT_OUTPUT_KEYS = [
    "DocumentExtractionAgent_output",
    "UnderwritingAgent_output",
    "PricingAgent_output",
    "LoanDecisionAgent_output",
]


def _parse_event_parts(event: Any) -> tuple[list[str], str | None, str | None]:
    """Parse a single event's parts to extract tool calls and text content."""
    content = getattr(event, "content", None)
    if not content:
        return [], None, None

    parts = getattr(content, "parts", None) or []
    role = getattr(content, "role", None)

    tool_names: list[str] = []
    user_text = None
    model_text = None

    for part in parts:
        function_call = getattr(part, "function_call", None)
        if function_call:
            tool_name = getattr(function_call, "name", None)
            if tool_name:
                tool_names.append(tool_name)

        text = getattr(part, "text", None)
        if text:
            if role == "user":
                user_text = text
            elif role == "model":
                model_text = text

    return tool_names, user_text, model_text


def _extract_tool_sequence_and_messages(
    callback_context: CallbackContext,
) -> tuple[list[str], str, str]:
    """Extract tool call sequence, user message, and final model response from current invocation."""
    invocation_context = callback_context._invocation_context
    current_invocation_id = getattr(invocation_context, "invocation_id", None)

    session = invocation_context.session
    events = session.events if session and session.events else []

    tool_sequence: list[str] = []
    final_response = ""
    user_message = ""

    for event in events:
        event_invocation_id = getattr(event, "invocation_id", None)
        if (
            current_invocation_id
            and event_invocation_id != current_invocation_id
        ):
            continue

        tools, user_text, model_text = _parse_event_parts(event)
        tool_sequence.extend(tools)
        if user_text:
            user_message = user_text
        if model_text:
            final_response = model_text

    return tool_sequence, final_response, user_message


def _collect_agent_outputs(callback_context: CallbackContext) -> dict:
    """Collect agent outputs from session state for grounding checks."""
    agent_outputs = {}
    for key in AGENT_OUTPUT_KEYS:
        value = callback_context.state.get(key)
        if value:
            if hasattr(value, "model_dump"):
                agent_outputs[key] = value.model_dump()
            elif isinstance(value, dict):
                agent_outputs[key] = value
            else:
                agent_outputs[key] = str(value)
    return agent_outputs


async def llm_judge_gate(
    callback_context: CallbackContext,
) -> genai_types.Content | None:
    """
    After-agent callback that validates the orchestrator's response before showing to user.

    Returns None to allow the response through, or Content with a blocked message.
    """
    request_id = callback_context.state.get("loan_request_id")

    try:
        tool_sequence, final_response, user_message = (
            _extract_tool_sequence_and_messages(callback_context)
        )
        agent_outputs = _collect_agent_outputs(callback_context)

        logger.info(f"LLM Judge evaluating response for {request_id}")
        logger.info(f"Tool sequence: {' -> '.join(tool_sequence)}")

        judge_input = JUDGE_PROMPT.format(
            agent_outputs=json.dumps(agent_outputs, indent=2, default=str)
            if agent_outputs
            else "No agent outputs",
            tool_sequence=" -> ".join(tool_sequence)
            if tool_sequence
            else "No tools called",
            user_message=user_message or "No user message",
            final_response=final_response or "No response",
        )

        location = os.getenv("GOOGLE_CLOUD_LOCATION")
        client = Client(
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=location if location is not None else "global",
        )
        judge_response = await client.aio.models.generate_content(
            model=JUDGE_MODEL,
            contents=judge_input,
            config=GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JudgeVerdict,
            ),
        )

        verdict_text = judge_response.text or ""
        verdict = JudgeVerdict.model_validate_json(verdict_text)

        log_msg = (
            f"LLM Judge {'APPROVED' if verdict.is_valid else 'BLOCKED'} response for {request_id} "
            f"(trajectory={verdict.trajectory_correct}, grounded={verdict.grounded_in_context})"
        )
        if verdict.is_valid:
            logger.info(log_msg)
        else:
            logger.warning(log_msg)
            logger.warning(f"Reasoning: {verdict.reasoning}")

        callback_context.state["_llm_judge_audit"] = {
            "loan_request_id": request_id,
            "verdict": verdict.model_dump(),
            "tool_sequence": tool_sequence,
        }

        if not verdict.is_valid:
            return genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        text=(
                            "I apologize, but I need to verify some information before providing a response. "
                            "This is to ensure accuracy in your loan processing request.\n\n"
                            f"Reference ID: {request_id or 'N/A'}\n\n"
                            "Please try your request again, or contact support if this issue persists."
                        )
                    )
                ],
            )

        return None

    except Exception as e:
        logger.error(f"LLM Judge error: {e}", exc_info=True)
        return genai_types.Content(
            role="model",
            parts=[
                genai_types.Part(
                    text=(
                        "I apologize, but I encountered an issue while validating the response. "
                        f"Please try your request again.\n\nReference: {request_id or 'N/A'}"
                    )
                )
            ],
        )
