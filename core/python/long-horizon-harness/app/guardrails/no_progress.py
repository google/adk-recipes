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

"""``NoProgressGuard`` — halts when the model emits identical text N turns in a row."""

from __future__ import annotations

from typing import Any

from google.adk.models import LlmResponse

from app.guardrails.halt_consumer import latch_halt

_STREAK_STATE_KEY = "__no_progress_streak__"
_LAST_TEXT_STATE_KEY = "__no_progress_last_text__"


def _normalize_text(llm_response: LlmResponse) -> str:
    content = getattr(llm_response, "content", None)
    if content is None or not content.parts:
        return ""
    text_parts = [
        part.text for part in content.parts if getattr(part, "text", None)
    ]
    if not text_parts:
        return ""
    return " ".join(" ".join(text_parts).split())


class NoProgressGuard:
    def __init__(self, *, window: int = 5) -> None:
        if window < 2:
            raise ValueError(
                f"window must be >= 2 (a window of 1 would halt on every "
                f"response); got {window}"
            )
        self.window = window

    async def after_model_callback(
        self,
        *,
        callback_context: Any,
        llm_response: LlmResponse,
    ) -> None:
        normalized = _normalize_text(llm_response)
        if not normalized:
            return None

        previous = callback_context.state.get(_LAST_TEXT_STATE_KEY)
        streak = callback_context.state.get(_STREAK_STATE_KEY, 0)

        if previous == normalized:
            streak += 1
        else:
            streak = 1

        callback_context.state[_LAST_TEXT_STATE_KEY] = normalized
        callback_context.state[_STREAK_STATE_KEY] = streak

        if streak >= self.window:
            latch_halt(
                callback_context.state,
                f"no progress detected: the model produced the same response "
                f"{streak} times in a row — halting to avoid a response loop.",
            )

        return None

    @staticmethod
    def reset(state: Any) -> None:
        """Clear the identical-response streak and the last-text marker.

        Same reason as ``RepeatedFailureGuard.reset``: if the streak
        counter is already at ``window`` when the halt is acknowledged,
        the very next identical response would re-trip the halt
        immediately.
        """
        state[_STREAK_STATE_KEY] = 0
        state[_LAST_TEXT_STATE_KEY] = None
