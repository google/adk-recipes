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

"""Turn a failed Gemini call into a short message the chat panel can show.

The A2A executor sends whatever text ends the turn to the dashboard as the
failed task's message. Left alone, that is `str(exc)` of the Gemini error: the
full response JSON, the service's internal debug info, and ADK's mitigation link,
which fills the chat window. `on_model_error` answers with one sentence instead
and logs the full error server-side, where the operator can still find it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from google.adk.models.llm_response import LlmResponse
from google.genai import errors as genai_errors

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest

logger = logging.getLogger(__name__)

_MAX_DETAIL_CHARS = 300


def on_model_error(
    *,
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse | None:
    """Replaces a Gemini API error with a user-facing message; re-raises the rest.

    Non-API errors propagate as internal bugs rather than service conditions.

    Args:
        callback_context: ADK callback context.
        llm_request: The outgoing LLM request.
        error: The exception raised by the model client.

    Returns:
        LlmResponse containing formatted error text, or None to propagate.
    """
    del callback_context, llm_request
    if not isinstance(error, genai_errors.APIError):
        return None
    logger.warning("chat model call failed: %s", error, exc_info=error)
    status = _format_status_label(error)
    return LlmResponse(
        error_code=status, error_message=render_user_message(error)
    )


def render_user_message(error: genai_errors.APIError) -> str:
    """Renders one or two user-facing sentences describing a model error.

    Args:
        error: The Gemini API error to format.

    Returns:
        Formatted error message suitable for chat display.
    """
    status = _format_status_label(error)
    if error.code == 429:
        return (
            f"The model is out of capacity right now ({status}). "
            "Wait a minute and send your message again."
        )
    if isinstance(error.code, int) and error.code >= 500:
        return f"The model service failed to answer ({status}). Try again in a moment."
    detail = (error.message or "").strip()
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[: _MAX_DETAIL_CHARS - 1].rstrip() + "…"
    return f"The model request failed ({status})" + (
        f": {detail}" if detail else "."
    )


def _format_status_label(error: genai_errors.APIError) -> str:
    return " ".join(str(part) for part in (error.code, error.status) if part)
