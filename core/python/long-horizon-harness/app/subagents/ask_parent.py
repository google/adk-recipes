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

"""``ask_parent`` — a delegated child's escalation channel to the parent.

A blocking ``delegate`` drives a resumable child. When the child hits a genuine
blocker only the parent can resolve, it calls ``ask_parent(question)``: the
question bubbles up the same resurfacing path an approval uses, the parent turn
answers (free text), and the child resumes in place with the answer. Outside a
resurfacing drain (background ``agent`` / routine, or no interactive channel)
there is no parent to ask, so it returns a graceful "use your best judgment"
answer rather than hanging. One escalation per delegated task (it spends the same
single bubble budget as an approval).
"""

from __future__ import annotations

from typing import Any

from app.subagents.resurface_context import (
    bubble_budget,
    try_consume_bubble,
)
from app.tools._hitl import request_user_confirmation

ASK_PARENT_TOOL_NAME = "ask_parent"

_NO_PARENT_NOTE = (
    "No parent is available to answer (background/headless run, or the single "
    "escalation for this task was already used). Make the most reasonable "
    "interpretation and note the assumption in your summary."
)


def _extract_answer(payload: Any) -> str | None:
    if isinstance(payload, dict):
        ans = payload.get("answer") or payload.get("choice")
        if isinstance(ans, str) and ans.strip():
            return ans.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def ask_parent(question: str, *, tool_context: Any) -> dict[str, Any]:
    """Escalate one blocking decision to the parent and get its answer.

    Use ONLY for a genuine blocker the parent must resolve — an ambiguous scope
    choice, a missing decision, a fork the task cannot sensibly default. Do NOT
    use it for trivia: make a reasonable assumption yourself and note it in your
    summary. You get ONE escalation per delegated task.

    Args:
        question: The decision you need, phrased so a one-line answer unblocks
            you.

    Returns:
        ``{"answer": <text>}`` once the parent replies; ``{"answer": None,
        "note": ...}`` when no parent is available or the parent declines.
    """
    if not question or not question.strip():
        return {"success": False, "error": "Question text is required."}
    question = question.strip()

    tc = getattr(tool_context, "tool_confirmation", None)
    if tc is not None:  # resumed — the parent answered
        if not bool(getattr(tc, "confirmed", False)):
            return {"answer": None, "note": "The parent declined to answer."}
        return {"answer": _extract_answer(getattr(tc, "payload", None))}

    # First call: bubble the question only if a parent turn can answer it.
    if bubble_budget() is None or not try_consume_bubble():
        return {"answer": None, "note": _NO_PARENT_NOTE}

    request_user_confirmation(
        tool_context,
        hint=question,
        payload={"kind": "ask_parent", "question": question},
    )
    return {
        "status": "awaiting_parent",
        "summary": f"Escalated to parent: {question}",
    }


__all__ = ["ASK_PARENT_TOOL_NAME", "ask_parent"]
