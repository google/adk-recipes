# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Single ``BasePlugin`` wrapping the three halt guards so they share a turn boundary."""

from __future__ import annotations

from typing import Any

from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins.base_plugin import BasePlugin

from app.guardrails.halt_consumer import halt_consumer_callback
from app.guardrails.no_progress import NoProgressGuard
from app.guardrails.repeated_failure import RepeatedFailureGuard


class GuardrailsPlugin(BasePlugin):
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        no_progress_window: int = 5,
    ) -> None:
        super().__init__(name="guardrails")
        self._repeated_failure = RepeatedFailureGuard(
            threshold=failure_threshold
        )
        self._no_progress = NoProgressGuard(window=no_progress_window)

    async def before_model_callback(
        self,
        *,
        callback_context: Any,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        return await halt_consumer_callback(
            callback_context=callback_context, llm_request=llm_request
        )

    async def after_model_callback(
        self,
        *,
        callback_context: Any,
        llm_response: LlmResponse,
    ) -> None:
        return await self._no_progress.after_model_callback(
            callback_context=callback_context, llm_response=llm_response
        )

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: Any,
        tool_context: Any,
        result: Any,
    ) -> None:
        # ADK plugin hooks use tool_args/result; the wrapped guard expects args/tool_response.
        return await self._repeated_failure.after_tool_callback(
            tool=tool,
            args=tool_args,
            tool_response=result,
            tool_context=tool_context,
        )


__all__ = ["GuardrailsPlugin"]
