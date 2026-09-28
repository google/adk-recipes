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

"""Session-service discovery helpers behind dream-review.

``list_active_users`` enumerates users with a session in the lookback
window (the nightly cron's "who do we dream about"), and
``_collect_user_sessions`` loads a single user's most recent sessions
with their events. Both read the session service, not the memory service.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.events.event import Event
from google.genai.types import Content, Part

from app.infrastructure.constants import (
    SCHEDULER_SOURCE,
    SESSION_SOURCE_KEY,
)

pytestmark = pytest.mark.asyncio


def _event(text: str, *, author: str = "user") -> Event:
    return Event(
        author=author,
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id="inv-test",
    )


class _FakeSessionService:
    """Duck-typed stand-in for ADK session services.

    ``list_sessions`` strips events (as the real services do); ``get_session``
    returns the full session with events.
    """

    def __init__(self, sessions: list[SimpleNamespace]) -> None:
        self._sessions = sessions

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> Any:
        del app_name
        selected = [
            SimpleNamespace(
                id=s.id,
                user_id=s.user_id,
                last_update_time=s.last_update_time,
                state=s.state,
            )
            for s in self._sessions
            if user_id is None or s.user_id == user_id
        ]
        return SimpleNamespace(sessions=selected)

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> Any:
        del app_name
        for s in self._sessions:
            if s.user_id == user_id and s.id == session_id:
                return s
        return None


def _session(
    sid: str,
    user_id: str,
    ts: float,
    events: list[Event] | None = None,
    *,
    source: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        user_id=user_id,
        last_update_time=ts,
        events=events or [],
        state={SESSION_SOURCE_KEY: source} if source else {},
    )


# =============================================================================
# list_active_users
# =============================================================================


async def test_list_active_users_returns_recent_distinct():
    from app.memory.dream_review import list_active_users

    service = _FakeSessionService(
        [
            _session("a1", "alice", 1000.0),
            _session("b1", "bob", 1000.0),
            _session("c1", "carol", 100.0),  # stale
        ]
    )
    users = await list_active_users(service, app_name="lha", since_ts=500.0)
    assert users == ["alice", "bob"]


async def test_list_active_users_dedups_per_user():
    from app.memory.dream_review import list_active_users

    service = _FakeSessionService(
        [
            _session("a1", "alice", 1000.0),
            _session("a2", "alice", 1200.0),
        ]
    )
    users = await list_active_users(service, app_name="lha", since_ts=500.0)
    assert users == ["alice"]


async def test_list_active_users_empty_when_none_recent():
    from app.memory.dream_review import list_active_users

    service = _FakeSessionService([_session("a1", "alice", 100.0)])
    users = await list_active_users(service, app_name="lha", since_ts=500.0)
    assert users == []


async def test_list_active_users_handles_service_without_list_api():
    from app.memory.dream_review import list_active_users

    users = await list_active_users(
        SimpleNamespace(), app_name="lha", since_ts=0.0
    )
    assert users == []


async def test_list_active_users_ignores_scheduler_sessions():
    """A user whose only recent session is dream-review's own is not active."""
    from app.memory.dream_review import list_active_users

    service = _FakeSessionService(
        [
            _session("d1", "alice", 1000.0, source=SCHEDULER_SOURCE),
            _session("b1", "bob", 1000.0),
        ]
    )
    users = await list_active_users(service, app_name="lha", since_ts=500.0)
    assert users == ["bob"]


async def test_list_active_users_counts_user_with_real_and_scheduler_session():
    from app.memory.dream_review import list_active_users

    service = _FakeSessionService(
        [
            _session("d1", "alice", 1000.0, source=SCHEDULER_SOURCE),
            _session("a1", "alice", 1000.0),
        ]
    )
    users = await list_active_users(service, app_name="lha", since_ts=500.0)
    assert users == ["alice"]


# =============================================================================
# _collect_user_sessions
# =============================================================================


async def test_collect_user_sessions_limits_and_orders_newest_first():
    from app.memory.dream_review import _collect_user_sessions

    service = _FakeSessionService(
        [
            _session("old", "alice", 100.0, [_event("oldest")]),
            _session("mid", "alice", 200.0, [_event("middle")]),
            _session("new", "alice", 300.0, [_event("newest")]),
        ]
    )
    out = await _collect_user_sessions(
        service, app_name="lha", user_id="alice", limit=2
    )
    assert out is not None
    assert [s["session_id"] for s in out] == ["new", "mid"]
    # Events are loaded via get_session, not the (stripped) list response.
    assert out[0]["events"][0].content.parts[0].text == "newest"


async def test_collect_user_sessions_only_target_user():
    from app.memory.dream_review import _collect_user_sessions

    service = _FakeSessionService(
        [
            _session("a1", "alice", 100.0, [_event("alice")]),
            _session("b1", "bob", 200.0, [_event("bob")]),
        ]
    )
    out = await _collect_user_sessions(
        service, app_name="lha", user_id="alice", limit=10
    )
    assert out is not None
    assert [s["session_id"] for s in out] == ["a1"]


async def test_collect_user_sessions_excludes_scheduler_sessions():
    """Dream-review's own persisted sessions are not fed back as input."""
    from app.memory.dream_review import _collect_user_sessions

    service = _FakeSessionService(
        [
            _session("real", "alice", 100.0, [_event("real chat")]),
            _session(
                "dream",
                "alice",
                300.0,
                [_event("dream")],
                source=SCHEDULER_SOURCE,
            ),
        ]
    )
    out = await _collect_user_sessions(
        service, app_name="lha", user_id="alice", limit=10
    )
    assert out is not None
    assert [s["session_id"] for s in out] == ["real"]


async def test_collect_user_sessions_none_when_no_list_api():
    from app.memory.dream_review import _collect_user_sessions

    out = await _collect_user_sessions(
        SimpleNamespace(), app_name="lha", user_id="alice", limit=5
    )
    assert out is None
