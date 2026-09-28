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

"""Resumability + forks integration tests.

Three layered contracts:

1. **App construction.** ``ResumabilityConfig(is_resumable=True)`` is
   experimental in ADK 2.0; opting in must not break ``App`` construction
   with our full callback chain or displace existing plugins.
2. **Clarify pause/resume shape.** A ``request_confirmation`` call (the
   canonical pause point resumability is designed for) enqueues into
   ``event.actions.requested_tool_confirmations``, and resuming with a
   ``tool_confirmation`` re-entry short-circuits the second request so
   the turn isn't re-paused.
3. **Throttle state survives pause/resume.** ``auto_capture_callback`` and
   ``review_fork_callback`` key their cooldown into session state. Across
   what would be a pause/resume boundary (session round-tripped through
   the session service), the throttle entries persist — so a back-to-back
   resumed turn is correctly skipped instead of double-flushing.

Assertions are on STATE and EVENT SHAPE only — no LLM content checks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService

from app.agent import app as root_app
from app.api.state import _latest_pending_confirmation
from app.memory import _throttle
from app.memory.auto_capture import auto_capture_callback
from app.memory.review_fork import review_fork_callback
from app.tools.clarify import clarify


def test_root_app_has_resumability_enabled() -> None:
    """The shipped ``App`` opts into resumability."""
    assert isinstance(root_app, App)
    assert root_app.resumability_config is not None
    assert root_app.resumability_config.is_resumable is True


def test_root_app_resumability_coexists_with_plugins() -> None:
    """``ResumabilityConfig`` doesn't displace the iteration-budget plugin."""
    plugin_names = {type(p).__name__ for p in (root_app.plugins or [])}
    assert "IterationBudgetPlugin" in plugin_names


def test_clarify_request_confirmation_payload_round_trips_through_lha_state() -> (
    None
):
    """Drive clarify the way ADK would — capture the ``request_confirmation``
    call into a fake event-actions surface, then verify
    ``_latest_pending_confirmation`` (the ``/lha/state`` slice helper)
    extracts the same payload back. This is the pause-and-resume contract
    minus the LLM.
    """
    captured: dict[str, dict | None] = {}

    requested_confirmations: dict[str, SimpleNamespace] = {}

    def fake_request_confirmation(*, hint=None, payload=None):
        captured["hint"] = hint
        captured["payload"] = payload
        requested_confirmations["fc-clarify-1"] = SimpleNamespace(
            hint=hint, payload=payload, confirmed=False
        )

    ctx = SimpleNamespace(
        state={},
        tool_confirmation=None,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=fake_request_confirmation,
    )

    first = clarify(
        question="continue?", choices=["yes", "no"], tool_context=ctx
    )
    assert first["status"] == "awaiting_user_response"
    assert ctx.actions.skip_summarization is True
    assert captured["hint"] == "continue?"
    assert captured["payload"] == {
        "question": "continue?",
        "choices": ["yes", "no"],
    }

    event = SimpleNamespace(
        content=None,
        actions=SimpleNamespace(
            requested_tool_confirmations=requested_confirmations
        ),
    )
    pending = _latest_pending_confirmation([event])
    assert pending == {
        "interrupt_id": "fc-clarify-1",
        "hint": "continue?",
        "payload": {"question": "continue?", "choices": ["yes", "no"]},
    }


def test_clarify_resume_with_tool_confirmation_short_circuits_request() -> None:
    """Resume path: ADK re-injects ``tool_confirmation`` with the user's
    answer. Clarify must NOT call ``request_confirmation`` again — that
    would re-pause an already-resumed turn."""

    request_calls = 0

    def fake_request_confirmation(*, hint=None, payload=None):
        nonlocal request_calls
        request_calls += 1

    ctx = SimpleNamespace(
        state={},
        tool_confirmation=SimpleNamespace(
            confirmed=True, payload={"answer": "yes"}
        ),
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=fake_request_confirmation,
    )

    result = clarify(
        question="continue?", choices=["yes", "no"], tool_context=ctx
    )
    assert result["status"] == "answered"
    assert result["answer"] == "yes"
    assert request_calls == 0


# =============================================================================
# Fork throttle survives the pause/resume boundary
# =============================================================================

# A turn that pauses on `request_confirmation` is later resumed by ADK; the
# session is checkpointed and re-loaded. If our throttle keyed off something
# transient (a module-global, a runner-local), a resumed back-to-back turn
# would double-flush. These tests pin that the throttle is keyed in session
# state — so it survives a session-service round-trip.


class _SessionStateCallbackContext:
    """Minimal callback-context wrapper that delegates ``state`` to a real
    ADK ``Session`` object and exposes the invocation_context fields the
    fork callbacks actually read."""

    def __init__(self, session: Any, memory_service: Any) -> None:
        self._session = session
        self.state = session.state
        self._invocation_context = SimpleNamespace(
            memory_service=memory_service,
            app_name=session.app_name,
            user_id=session.user_id,
            session=session,
        )
        self.add_session_to_memory_calls = 0

    async def add_session_to_memory(self) -> None:
        self.add_session_to_memory_calls += 1


async def _commit_state(
    session_service: Any, session: Any, keys: list[str]
) -> None:
    """Mirror ADK's end-of-invocation state commit for the given keys.

    Why: ``InMemorySessionService.get_session()`` returns a fresh state
    snapshot — direct mutations made by a callback are not visible after
    reload. Production ADK commits via ``append_event(state_delta=...)``
    at the end of each invocation; these tests have to do the same to
    exercise the real pause/resume contract.
    """
    state_delta = {
        key: session.state[key] for key in keys if key in session.state
    }
    if not state_delta:
        return
    event = Event(
        invocation_id="test-commit",
        author="test",
        actions=EventActions(state_delta=state_delta),
    )
    await session_service.append_event(session, event)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(_throttle.time, "time", lambda: clock["now"])
    return clock


@pytest.mark.asyncio
async def test_auto_capture_throttle_state_persists_across_session_round_trip(
    fake_clock: dict[str, float],
) -> None:
    """After turn N flushes via ``auto_capture_callback``, the throttle
    entry must be retrievable via ``session_service.get_session(...)`` —
    that is the only way the resumed turn will see it."""
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    session = await session_service.create_session(
        app_name="parent", user_id="alice"
    )

    ctx = _SessionStateCallbackContext(session, memory_service)
    await auto_capture_callback(ctx)  # type: ignore[arg-type]
    assert ctx.add_session_to_memory_calls == 1

    # Commit the callback's state mutation the way ADK does at end-of-
    # invocation, then round-trip the session through the service (the
    # resume contract: ADK reloads from its store before the resumed turn).
    await _commit_state(session_service, session, ["_fork_throttle"])
    reloaded = await session_service.get_session(
        app_name="parent", user_id="alice", session_id=session.id
    )
    assert reloaded is not None
    assert "_fork_throttle" in reloaded.state
    assert reloaded.state["_fork_throttle"]["auto_capture"]["count"] == 1


@pytest.mark.asyncio
async def test_resumed_turn_within_cooldown_skips_double_flush(
    fake_clock: dict[str, float],
) -> None:
    """A turn that pauses on clarify and resumes seconds later must not
    re-trigger ``add_session_to_memory`` — otherwise the resumed turn
    silently double-writes the session into Memory Bank."""
    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    session = await session_service.create_session(
        app_name="parent", user_id="alice"
    )

    ctx1 = _SessionStateCallbackContext(session, memory_service)
    await auto_capture_callback(ctx1)  # type: ignore[arg-type]
    assert ctx1.add_session_to_memory_calls == 1
    await _commit_state(session_service, session, ["_fork_throttle"])

    # The clarify pause/resume cycle would advance wallclock by a few
    # seconds at most; well inside the 120s cooldown.
    fake_clock["now"] += 5.0
    reloaded = await session_service.get_session(
        app_name="parent", user_id="alice", session_id=session.id
    )
    assert reloaded is not None
    ctx2 = _SessionStateCallbackContext(reloaded, memory_service)
    await auto_capture_callback(ctx2)  # type: ignore[arg-type]
    assert ctx2.add_session_to_memory_calls == 0


@pytest.mark.asyncio
async def test_review_fork_throttle_survives_session_round_trip(
    fake_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``review_fork_callback`` spawns a fork via ``asyncio.create_task``.
    Verify the throttle slot is claimed on first call AND skipped on the
    next call after a session reload — i.e. the throttle is keyed in
    session state, not in module-globals.
    """
    spawned: list[Any] = []

    def fake_create_task(coro: Any) -> Any:
        spawned.append(coro)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    session_service = InMemorySessionService()
    memory_service = InMemoryMemoryService()
    session = await session_service.create_session(
        app_name="parent", user_id="alice"
    )

    ctx1 = _SessionStateCallbackContext(session, memory_service)
    await review_fork_callback(ctx1)
    assert len(spawned) == 1
    await _commit_state(session_service, session, ["_fork_throttle"])

    fake_clock["now"] += 5.0
    reloaded = await session_service.get_session(
        app_name="parent", user_id="alice", session_id=session.id
    )
    assert reloaded is not None
    ctx2 = _SessionStateCallbackContext(reloaded, memory_service)
    await review_fork_callback(ctx2)
    assert len(spawned) == 1, "throttle must skip the resumed-turn spawn"
