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

"""Shared HITL (human-in-the-loop) confirmation helper for tools.

Every tool that gates on a user prompt — ``bash``, ``clarify``, future
confirm-tier tools — needs the same plumbing: check ``tool_confirmation``;
if missing, dispatch ``request_confirmation(...)`` and mark
``actions.skip_summarization`` so the kickoff response isn't paraphrased.

This helper performs only the request side. Callers still decide what to
do with the returned ``ToolConfirmation`` (pending / declined / confirmed)
since the success/decline envelopes differ per tool.
"""

from __future__ import annotations

from typing import Any


def request_user_confirmation(
    tool_context: Any,
    *,
    hint: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    """Return the resolved ``ToolConfirmation`` or ``None`` if pending.

    When there is no confirmation yet, dispatches the ADK
    ``request_confirmation`` call and toggles ``actions.skip_summarization``
    so ADK does not paraphrase the pending-state response shown to the user.
    """
    tc = getattr(tool_context, "tool_confirmation", None)
    if tc is not None:
        return tc
    request = getattr(tool_context, "request_confirmation", None)
    if callable(request):
        kwargs: dict[str, Any] = {"hint": hint}
        if payload is not None:
            kwargs["payload"] = payload
        request(**kwargs)
    actions = getattr(tool_context, "actions", None)
    if actions is not None:
        try:
            actions.skip_summarization = True
        except AttributeError:
            pass
    return None
