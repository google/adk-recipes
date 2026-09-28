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

"""Clarify tool — ADK HITL question/answer surface.

First call: invoke ``tool_context.request_confirmation(hint=question,
payload={...})`` so ADK enqueues the pending confirmation in
``event_actions.requested_tool_confirmations[function_call_id]``. The
front-end renders the question, captures the user's answer, and resumes
the tool call with a ``ToolConfirmation`` whose ``payload`` carries the
answer (or ``confirmed=False`` to decline).
"""

from __future__ import annotations

from typing import Any

from app.tools._hitl import request_user_confirmation

MAX_CHOICES = 4


def _normalize_choices(choices: list[str] | None) -> list[str] | None:
    if choices is None:
        return None
    if not isinstance(choices, list):
        raise TypeError("choices must be a list of strings.")
    cleaned = [str(c).strip() for c in choices if str(c).strip()]
    if not cleaned:
        return None
    return cleaned[:MAX_CHOICES]


def _extract_answer(payload: Any) -> str | None:
    if isinstance(payload, dict):
        ans = payload.get("answer") or payload.get("choice")
        if isinstance(ans, str) and ans.strip():
            return ans.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def clarify(
    question: str,
    choices: list[str] | None = None,
    *,
    tool_context: Any,
) -> dict[str, Any]:
    """Ask the user a question, optionally with up to 4 predefined choices.

    Use when the task is ambiguous and a reasonable default isn't obvious.
    Do NOT use for low-stakes decisions — pick a default yourself.

    Args:
        question: The text to present to the user.
        choices: Up to 4 predefined answer choices. Omit for open-ended.
    """
    if not question or not question.strip():
        return {"success": False, "error": "Question text is required."}

    question = question.strip()

    try:
        normalized = _normalize_choices(choices)
    except TypeError as exc:
        return {"success": False, "error": str(exc)}

    tc = request_user_confirmation(
        tool_context,
        hint=question,
        payload={"question": question, "choices": normalized},
    )
    if tc is None:
        return {
            "success": True,
            "question": question,
            "choices": normalized,
            "status": "awaiting_user_response",
        }

    if not getattr(tc, "confirmed", False):
        return {
            "success": False,
            "question": question,
            "choices": normalized,
            "status": "declined",
            "error": "User declined to answer.",
        }

    answer = _extract_answer(getattr(tc, "payload", None))
    return {
        "success": True,
        "question": question,
        "choices": normalized,
        "answer": answer,
        "status": "answered",
    }
