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

"""``_slice_state`` pulls ``pending_confirmation`` from event actions.

When ``clarify`` (or any other HITL tool) calls
``tool_context.request_confirmation(...)``, ADK enqueues a
``ToolConfirmation`` in ``event.actions.requested_tool_confirmations``
keyed by ``function_call_id``. The chat UI reads this via
``GET /lha/state`` to know there's a pending question to render. The
extraction walks newest-first and only returns the latest unresolved
request — once a later event answers it, the slice clears.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.api.state import _latest_pending_confirmation, _slice_state


def _event_with_request(
    fcid: str, *, hint: str = "?", payload: Any = None
) -> SimpleNamespace:
    confirmation = SimpleNamespace(hint=hint, payload=payload, confirmed=False)
    return SimpleNamespace(
        content=None,
        actions=SimpleNamespace(
            requested_tool_confirmations={fcid: confirmation}
        ),
    )


def _event_with_response_carrying_confirmation(
    fcid: str, *, tool_name: str = "adk_request_confirmation"
) -> SimpleNamespace:
    """Resume event — function_response shape ADK emits when the user
    answers and the runner re-injects the ToolConfirmation."""
    fr = SimpleNamespace(
        id=fcid,
        name=tool_name,
        response={"tool_confirmation": {"confirmed": True}},
    )
    part = SimpleNamespace(function_response=fr)
    return SimpleNamespace(
        content=SimpleNamespace(parts=[part]),
        actions=SimpleNamespace(requested_tool_confirmations={}),
    )


def test_empty_events_returns_none():
    assert _latest_pending_confirmation(None) is None
    assert _latest_pending_confirmation([]) is None


def test_unresolved_request_returns_payload():
    events = [
        _event_with_request(
            "fc-1", hint="continue?", payload={"choices": ["yes", "no"]}
        )
    ]
    pending = _latest_pending_confirmation(events)
    assert pending == {
        "interrupt_id": "fc-1",
        "hint": "continue?",
        "payload": {"choices": ["yes", "no"]},
    }


def test_resolved_request_returns_none():
    events = [
        _event_with_request("fc-1", hint="?"),
        _event_with_response_carrying_confirmation("fc-1"),
    ]
    assert _latest_pending_confirmation(events) is None


def test_latest_pending_wins_when_multiple_unresolved():
    events = [
        _event_with_request("fc-1", hint="first"),
        _event_with_request("fc-2", hint="second"),
    ]
    pending = _latest_pending_confirmation(events)
    assert pending["interrupt_id"] == "fc-2"
    assert pending["hint"] == "second"


def test_slice_state_exposes_pending_confirmation_field():
    events = [_event_with_request("fc-9", hint="why?", payload=None)]
    sliced = _slice_state({}, events=events)
    assert "pending_confirmation" in sliced
    assert sliced["pending_confirmation"]["interrupt_id"] == "fc-9"


def test_slice_state_without_events_has_null_pending():
    sliced = _slice_state({}, events=None)
    assert sliced["pending_confirmation"] is None
