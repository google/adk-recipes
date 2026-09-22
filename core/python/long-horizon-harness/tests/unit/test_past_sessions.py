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

"""Unit tests for session_search.

Cover user scoping, current-session exclusion, title derivation, event
extraction, drill-mode refusal of the current session, and missing-session
handling. Drives ``recall_past_sessions_entries`` directly so no Runner is
needed.
"""

from __future__ import annotations

import pytest
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from horizon.tools.past_sessions import (
    EVENT_TEXT_MAX_LEN,
    recall_past_sessions_entries,
)

APP_NAME = "test_app"
USER_A = "user_a"
USER_B = "user_b"


def _user_event(text: str) -> Event:
    return Event(
        invocation_id="inv",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text=text)]),
    )


def _agent_event(text: str) -> Event:
    return Event(
        invocation_id="inv",
        author="root_agent",
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


async def _seed_session(
    svc: InMemorySessionService,
    *,
    user_id: str,
    user_text: str,
    agent_text: str | None = None,
):
    session = await svc.create_session(app_name=APP_NAME, user_id=user_id)
    await svc.append_event(session, _user_event(user_text))
    if agent_text is not None:
        await svc.append_event(session, _agent_event(agent_text))
    return session


@pytest.mark.asyncio
async def test_index_lists_other_sessions_for_same_user():
    svc = InMemorySessionService()
    s1 = await _seed_session(svc, user_id=USER_A, user_text="build me tetris")
    s2 = await _seed_session(svc, user_id=USER_A, user_text="help with taxes")
    current = await _seed_session(
        svc, user_id=USER_A, user_text="what did I ask before?"
    )

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
    )

    assert result["success"] is True
    assert result["mode"] == "index"
    ids = {s["session_id"] for s in result["sessions"]}
    assert ids == {s1.id, s2.id}
    titles = {s["session_id"]: s["title"] for s in result["sessions"]}
    assert titles[s1.id] == "build me tetris"
    assert titles[s2.id] == "help with taxes"


@pytest.mark.asyncio
async def test_index_isolates_users():
    svc = InMemorySessionService()
    await _seed_session(svc, user_id=USER_B, user_text="user b secret")
    current = await _seed_session(svc, user_id=USER_A, user_text="hello")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
    )

    assert result["sessions"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_drill_returns_user_and_assistant_text():
    svc = InMemorySessionService()
    target = await _seed_session(
        svc,
        user_id=USER_A,
        user_text="make me tetris in python",
        agent_text="Sure, here's tetris.py",
    )
    current = await _seed_session(svc, user_id=USER_A, user_text="recall")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
        session_id=target.id,
    )

    assert result["success"] is True
    assert result["mode"] == "drill"
    assert result["session_id"] == target.id
    authors = [e["author"] for e in result["events"]]
    texts = [e["text"] for e in result["events"]]
    assert authors == ["user", "assistant"]
    assert texts == ["make me tetris in python", "Sure, here's tetris.py"]


@pytest.mark.asyncio
async def test_drill_refuses_current_session():
    svc = InMemorySessionService()
    current = await _seed_session(svc, user_id=USER_A, user_text="hi")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
        session_id=current.id,
    )

    assert result["success"] is False
    assert "current session" in result["error"].lower()


@pytest.mark.asyncio
async def test_drill_missing_session():
    svc = InMemorySessionService()
    current = await _seed_session(svc, user_id=USER_A, user_text="hi")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
        session_id="does-not-exist",
    )

    assert result["success"] is False
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_drill_truncates_long_event_text():
    svc = InMemorySessionService()
    long_text = "x" * (EVENT_TEXT_MAX_LEN + 200)
    target = await _seed_session(svc, user_id=USER_A, user_text=long_text)
    current = await _seed_session(svc, user_id=USER_A, user_text="now")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
        session_id=target.id,
    )

    text = result["events"][0]["text"]
    assert text.endswith("…")
    assert len(text) == EVENT_TEXT_MAX_LEN + 1  # truncated + ellipsis


@pytest.mark.asyncio
async def test_index_respects_explicit_title_state_key():
    svc = InMemorySessionService()
    titled = await svc.create_session(
        app_name=APP_NAME,
        user_id=USER_A,
        state={"lha_chat_title": "My Custom Title"},
    )
    await svc.append_event(titled, _user_event("original text"))
    current = await _seed_session(svc, user_id=USER_A, user_text="now")

    result = await recall_past_sessions_entries(
        session_service=svc,
        app_name=APP_NAME,
        user_id=USER_A,
        current_session_id=current.id,
    )

    by_id = {s["session_id"]: s for s in result["sessions"]}
    assert by_id[titled.id]["title"] == "My Custom Title"
