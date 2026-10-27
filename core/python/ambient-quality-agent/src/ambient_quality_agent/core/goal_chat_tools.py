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

"""Chat tool for saving or removing the developer's goal, with the user's approval.

`set_goal` asks through ADK's tool confirmation: the first call requests it and
writes nothing; ADK runs the call again with the user's answer once they
approve or decline on the dashboard's card. ADK refuses to run it again with
arguments other than the ones in the session history, so the text saved is
the text the card showed. The save itself is the same one the Configuration
page uses.
"""

from __future__ import annotations

from typing import Any

from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.orchestrator import document_tools
from google.adk.tools import ToolContext


async def set_goal(goal: str, tool_context: ToolContext) -> dict[str, Any]:
    """Saves the developer's goal, or removes it when `goal` is empty.

    Call only when the user asks to save, change or remove the goal, with the
    complete text they agreed to; never on your own initiative. The dashboard
    shows the user the text and asks them to approve it, and nothing is written
    until they do. A removed goal stays a version the user can restore on the
    Configuration page.

    Args:
        goal: The complete goal text, exactly as agreed with the user; empty to
            remove the goal.

    Returns:
        ``saved`` True with the stored ``goal`` and its ``version`` (None when
        removed); or ``saved`` False with a ``reason`` while awaiting approval
        or after a decline; or an ``error``.
    """
    removing = not goal.strip()
    if len(goal.strip().encode("utf-8")) > goal_doc.GOAL_MAX_BYTES:
        return {
            "error": f"The goal is longer than {goal_doc.GOAL_MAX_BYTES // 1024} "
            "KiB; shorten it before asking the user to approve it."
        }
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(
            hint="Remove the developer goal?"
            if removing
            else "Save this as the developer goal?"
        )
        # No skip_summarization, unlike ADK's own confirmation path: it adds the
        # result to the history as text, and Gemini then refuses the resumed
        # request ("Requests ending with a model turn are not supported").
        return {
            "saved": False,
            "reason": "Waiting for the user to approve the goal on the card the "
            "dashboard shows, which has the full text; do not repeat the text. "
            "It is not saved yet.",
        }
    if not confirmation.confirmed:
        return {
            "saved": False,
            "reason": "The user declined; nothing was saved.",
        }
    result = await document_tools.set_goal(tool_context, goal=goal)
    if "error" in result:
        return {"saved": False, "error": result["error"]}
    return {"saved": True, **result}
