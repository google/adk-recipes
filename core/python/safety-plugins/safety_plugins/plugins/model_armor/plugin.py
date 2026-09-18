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

"""ADK callbacks for screening user text, model text and tool results."""

import asyncio
import json
import math
import os
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .client import ModelArmorClient
from .constants import (
    SDP_FILTER,
    UNSAFE_PROMPT_STATE_KEY,
    USER_PROMPT_REMOVED_MESSAGE,
    ModelArmorAction,
    ModelArmorMethod,
    ModelArmorResponse,
    ScreeningStage,
)
from .logs import (
    record_screening,
    record_screening_error,
)
from .policy import Decision, block_message, decide
from .response import validate_response

_APPLICATION_TIMEOUT_GRACE_S = 1.0


def _content_text(content: types.Content) -> str:
    """Every text part, so nothing escapes screening by hiding in part two."""
    return "\n".join(
        part.text for part in (content.parts or []) if part.text is not None
    )


def _redaction_indexes(
    content: types.Content, *, user_message: bool = False
) -> list[int]:
    """Locate text that can safely receive one aggregate SDP replacement.

    Model Armor returns no part offsets. Only plain text can be rewritten;
    thought text and signed parts must never be flattened into public text.
    Multiple parts can collapse only in a text-only user message.
    """
    parts = content.parts or []
    indexes = [i for i, part in enumerate(parts) if part.text is not None]
    for i in indexes:
        part = parts[i]
        if part.thought or part.model_dump(
            exclude_none=True, exclude={"text", "thought"}
        ):
            return []
    if len(indexes) == 1 or (user_message and len(indexes) == len(parts)):
        return indexes
    return []


def _redact(
    content: types.Content, text: str, indexes: list[int]
) -> types.Content:
    if len(indexes) == 1:
        index = indexes[0]
        content.parts[index] = content.parts[index].model_copy(
            update={"text": text}
        )
        return content
    return _replace(content, text)


def _replace(content: types.Content, text: str) -> types.Content:
    content.parts = [types.Part.from_text(text=text)]
    return content


def _model_message(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)],
        )
    )


def _json_payload_text(payload: dict[str, Any]) -> str:
    """Serialize structured data so DLP can return a parseable replacement."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _redacted_tool_result(text: str) -> dict[str, Any]:
    """Restore a transformed tool result without ever returning the original."""
    try:
        transformed = json.loads(text)
    except json.JSONDecodeError:
        transformed = text
    if isinstance(transformed, dict):
        return transformed
    return {"redacted_output": transformed}


class ModelArmorSafetyFilterPlugin(BasePlugin):
    """Screen prompts, callback inputs, model responses and tool outputs."""

    def __init__(
        self,
        project_id: str | None = None,
        location_id: str | None = None,
        template_id: str | None = None,
        *,
        client: ModelArmorClient | None = None,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(name="ModelArmorPlugin")
        self.timeout_s = (
            float(os.environ.get("MODEL_ARMOR_TIMEOUT_S", "5"))
            if timeout_s is None
            else timeout_s
        )
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("Model Armor timeout must be finite and positive")
        self.client = (
            client
            if client is not None
            else ModelArmorClient(
                project_id=project_id
                or os.environ.get("GOOGLE_CLOUD_MODEL_ARMOR_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                location_id=location_id
                or os.environ.get("GOOGLE_CLOUD_MODEL_ARMOR_LOCATION")
                or os.environ.get("GOOGLE_CLOUD_LOCATION", ""),
                template_id=template_id
                or os.environ.get("MODEL_ARMOR_TEMPLATE_ID", ""),
            )
        )

    async def close(self) -> None:
        """Release the Model Armor transport when the ADK runner closes."""
        await self.client.close()

    async def scan(
        self,
        stage: ScreeningStage,
        text: str,
        *,
        tool_name: str | None = None,
    ) -> ModelArmorResponse | None:
        """Screen ``text``, or return ``None`` if it could not be screened.

        The async Google RPC receives its own transport deadline. A slightly
        larger application guard also cancels the coroutine if the client does
        not honor that deadline. Every failure collapses to ``None``, which the
        policy reads as "block".
        """
        method = (
            ModelArmorMethod.SANITIZE_USER_PROMPT
            if stage
            in {
                ScreeningStage.USER_PROMPT,
                ScreeningStage.CALLBACK_INPUT,
                ScreeningStage.TOOL_OUTPUT,
            }
            else ModelArmorMethod.SANITIZE_MODEL_RESPONSE
        )
        response: ModelArmorResponse | None = None
        try:
            response = await asyncio.wait_for(
                self.client.sanitize(method, text, timeout_s=self.timeout_s),
                timeout=self.timeout_s + _APPLICATION_TIMEOUT_GRACE_S,
            )
            validate_response(response)
            return response
        except Exception as exc:
            # Keep verdict metadata from rejected scans; exclude Google's
            # free-form diagnostic messages and the exception's message.
            record_screening_error(
                stage=stage,
                method=method,
                error=exc,
                response=response,
                tool_name=tool_name,
            )
            return None

    async def _screen(
        self,
        stage: ScreeningStage,
        text: str,
        *,
        tool_name: str | None = None,
        redaction_supported: bool = True,
    ) -> Decision:
        """Scan, decide, and record through one path for every stage."""
        # Nothing to screen. An image-only or whitespace-only message carries
        # no text for any filter to match, so skip the RPC rather than send
        # Model Armor an empty payload it may reject — which would fail closed
        # and halt a perfectly legitimate turn.
        if not text.strip():
            return Decision(action=ModelArmorAction.ALLOW)

        response = await self.scan(stage, text, tool_name=tool_name)
        decision = decide(response)
        if (
            decision.action == ModelArmorAction.REDACT
            and not redaction_supported
        ):
            decision = Decision(
                action=ModelArmorAction.BLOCK, reasons=(SDP_FILTER,)
            )
        # A failed scan was already recorded by ``scan``.
        if response is not None:
            record_screening(
                stage,
                response,
                action=decision.action,
                reasons=decision.reasons,
                tool_name=tool_name,
            )
        return decision

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        stage = ScreeningStage.USER_PROMPT
        indexes = _redaction_indexes(user_message, user_message=True)
        decision = await self._screen(
            stage,
            _content_text(user_message),
            redaction_supported=bool(indexes),
        )

        if decision.action == ModelArmorAction.ALLOW:
            return None
        if decision.action == ModelArmorAction.REDACT:
            return _redact(user_message, decision.redacted_text or "", indexes)

        # Replace in place before ADK persists the prompt. The temporary
        # marker halts the invocation before any agent or model executes.
        message = block_message(stage, decision)
        invocation_context.session.state[UNSAFE_PROMPT_STATE_KEY] = message
        return _replace(user_message, message)

    async def before_run_callback(
        self,
        *,
        invocation_context: InvocationContext,
    ) -> types.Content | None:
        marker = invocation_context.session.state.pop(
            UNSAFE_PROMPT_STATE_KEY, None
        )
        if marker:
            return types.ModelContent(marker)
        return None

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Also halt runtimes which do not use the runner's early exit."""
        marker = callback_context.state.get(UNSAFE_PROMPT_STATE_KEY)
        if marker:
            # Consume it: a retry or a sub-agent call later in the same invocation
            # must not be halted by a stale marker.
            callback_context.state[UNSAFE_PROMPT_STATE_KEY] = False
            return _model_message(
                marker
                if isinstance(marker, str)
                else USER_PROMPT_REMOVED_MESSAGE
            )

        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        if llm_response.content is None:
            return None

        stage = ScreeningStage.MODEL_RESPONSE
        indexes = _redaction_indexes(llm_response.content)
        decision = await self._screen(
            stage,
            _content_text(llm_response.content),
            redaction_supported=bool(indexes),
        )
        if decision.action == ModelArmorAction.BLOCK:
            return _model_message(block_message(stage, decision))
        if decision.action == ModelArmorAction.REDACT:
            _redact(llm_response.content, decision.redacted_text or "", indexes)
            return llm_response
        return None

    async def screen_external_payload(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Screen untrusted external data before it can enter agent state."""
        decision = await self._screen(
            ScreeningStage.CALLBACK_INPUT,
            _json_payload_text(payload),
        )
        if decision.action == ModelArmorAction.BLOCK:
            return None
        if decision.action == ModelArmorAction.ALLOW:
            return payload
        try:
            transformed = json.loads(decision.redacted_text or "")
        except json.JSONDecodeError:
            return None
        return transformed if isinstance(transformed, dict) else None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        stage = ScreeningStage.TOOL_OUTPUT
        decision = await self._screen(
            stage,
            _json_payload_text(result),
            tool_name=tool.name,
        )
        if decision.action == ModelArmorAction.BLOCK:
            return {"error": block_message(stage, decision)}
        if decision.action == ModelArmorAction.REDACT:
            return _redacted_tool_result(decision.redacted_text or "")
        return None
