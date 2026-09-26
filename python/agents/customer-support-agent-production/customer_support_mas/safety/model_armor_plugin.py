"""ADK plugin that screens prompts and responses through Google Cloud Model Armor."""

import logging
import os
from typing import Any

from google.adk.agents import invocation_context, llm_agent
from google.adk.events import event
from google.adk.models import llm_request, llm_response
from google.adk.plugins import base_plugin
from google.adk.tools import base_tool, tool_context
from google.api_core.client_options import ClientOptions
from google.cloud import modelarmor_v1
from google.genai import types

from customer_support_mas.safety.safety_util import parse_model_armor_response

logger = logging.getLogger(__name__)

Event = event.Event
InvocationContext = invocation_context.InvocationContext
CallbackContext = base_plugin.CallbackContext
ToolContext = tool_context.ToolContext
LlmAgent = llm_agent.LlmAgent
BasePlugin = base_plugin.BasePlugin
BaseTool = base_tool.BaseTool
LlmRequest = llm_request.LlmRequest
LlmResponse = llm_response.LlmResponse

_USER_PROMPT_BLOCKED_MESSAGE = "BLOCKED: Unsafe prompt detected."
_MODEL_RESPONSE_BLOCKED_MESSAGE = "BLOCKED: Unsafe model response detected."
_TOOL_OUTPUT_BLOCKED_MESSAGE = "Unable to emit tool result due to unsafe content."


class ModelArmorSafetyFilterPlugin(BasePlugin):
    """ADK plugin that screens user prompts, model responses, and tool outputs
    through Google Cloud Model Armor before they are processed or returned.
    """

    def __init__(
        self,
        template_id: str,
        project_id: str = os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location_id: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
    ) -> None:
        super().__init__(name="ModelArmorSafetyFilterPlugin")
        self._project_id = project_id
        self._location_id = location_id or "us-central1"
        self._template_id = template_id

        # Accept both full resource name and short ID
        if template_id.startswith("projects/"):
            self._template_resource_name = template_id
        else:
            self._template_resource_name = (
                f"projects/{self._project_id}/locations/{self._location_id}/templates/{template_id}"
            )

        self._client = modelarmor_v1.ModelArmorClient(
            client_options=ClientOptions(api_endpoint=f"modelarmor.{self._location_id}.rep.googleapis.com")
        )
        logger.info("ModelArmorSafetyFilterPlugin initialized with template: %s", self._template_resource_name)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _check_user_prompt(self, text: str) -> list[str] | None:
        try:
            response = self._client.sanitize_user_prompt(
                request=modelarmor_v1.SanitizeUserPromptRequest(
                    name=self._template_resource_name,
                    user_prompt_data=modelarmor_v1.DataItem(text=text),
                )
            )
            return parse_model_armor_response(response)
        except Exception as e:
            logger.error("Model Armor sanitize_user_prompt failed: %s", e)
            return None  # fail open

    def _check_model_response(self, text: str) -> list[str] | None:
        try:
            response = self._client.sanitize_model_response(
                request=modelarmor_v1.SanitizeModelResponseRequest(
                    name=self._template_resource_name,
                    model_response_data=modelarmor_v1.DataItem(text=text),
                )
            )
            return parse_model_armor_response(response)
        except Exception as e:
            logger.error("Model Armor sanitize_model_response failed: %s", e)
            return None  # fail open

    # -------------------------------------------------------------------------
    # ADK Callbacks
    # -------------------------------------------------------------------------

    async def on_user_message_callback(
        self,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        """Screen the incoming user message. Sets session state if unsafe so
        before_run_callback can halt execution before any LLM call."""
        if not user_message.parts or not user_message.parts[0].text:
            return None

        violations = self._check_user_prompt(user_message.parts[0].text)
        if violations:
            logger.warning("Unsafe user prompt detected: %s", violations)
            invocation_context.session.state["is_user_prompt_safe"] = False
            invocation_context.session.state["user_prompt_violations"] = str(violations)
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"{_USER_PROMPT_BLOCKED_MESSAGE} Violations: {violations}")],
            )
        invocation_context.session.state["is_user_prompt_safe"] = True
        return None

    async def before_run_callback(
        self,
        invocation_context: InvocationContext,
    ) -> types.Content | None:
        """Halt the invocation if the user prompt was flagged as unsafe."""
        if not invocation_context.session.state.get("is_user_prompt_safe", True):
            violations = invocation_context.session.state.get("user_prompt_violations", "Unknown")
            # Reset state for the next turn
            invocation_context.session.state["is_user_prompt_safe"] = True
            return types.Content(
                role="model",
                parts=[types.Part.from_text(text=f"{_USER_PROMPT_BLOCKED_MESSAGE} Violations: {violations}")],
            )
        return None

    async def after_model_callback(
        self,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Screen the model's response before it is returned to the user."""
        llm_content = llm_response.content
        if not llm_content or not llm_content.parts:
            return None

        model_text = "\n".join(part.text or "" for part in llm_content.parts).strip()
        if not model_text:
            return None

        violations = self._check_model_response(model_text)
        if violations:
            logger.warning("Unsafe model response detected: %s", violations)
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"{_MODEL_RESPONSE_BLOCKED_MESSAGE} Violations: {violations}")],
                )
            )
        return None

    async def after_tool_callback(
        self,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Screen tool outputs before they are fed back to the model."""
        violations = self._check_user_prompt(str(result))
        if violations:
            logger.warning("Unsafe tool output detected from '%s': %s", tool.name, violations)
            return {"error": f"{_TOOL_OUTPUT_BLOCKED_MESSAGE} Violations: {violations}"}
        return None
