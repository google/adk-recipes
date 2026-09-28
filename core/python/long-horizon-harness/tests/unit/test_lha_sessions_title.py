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

"""Sidebar titles must derive from the FIRST user message, never a recent one.

TITLE_KEY is set only by the rename endpoint, so an un-renamed chat derives its
title by scanning events for the first user message. The list view fetches a
bounded recent-events window for perf; that window keeps the MOST RECENT events,
so a long un-renamed chat would otherwise show a recent message as its title.
These tests pin the title to the oldest user message regardless of how many
later events exist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.api.sessions import TITLE_KEY, attach_session_routes
from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME

USER_ID = "u@local"


@dataclass
class _StubRunner:
    session_service: InMemorySessionService = field(
        default_factory=InMemorySessionService
    )


def _build_app(runner: _StubRunner) -> FastAPI:
    app = FastAPI()
    attach_session_routes(app, runner=runner)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


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


async def _seed_long_untitled(
    runner: _StubRunner, first: str, n_more: int
) -> str:
    svc = runner.session_service
    session = await svc.create_session(app_name=APP_NAME, user_id=USER_ID)
    await svc.append_event(session, _user_event(first))
    # Many later turns; their text must NOT win the title.
    for i in range(n_more):
        await svc.append_event(session, _agent_event(f"answer {i}"))
        await svc.append_event(session, _user_event(f"follow-up {i}"))
    return session.id


def test_untitled_long_session_titles_from_first_user_message() -> None:
    runner = _StubRunner()
    sid = asyncio.run(
        _seed_long_untitled(runner, "FIRST original question", n_more=20)
    )

    client = TestClient(_build_app(runner))
    rows = client.get("/lha/sessions?source=user").json()

    assert len(rows) == 1
    assert rows[0]["title"] == "FIRST original question", rows[0]["title"]
    # And the recent follow-ups must NOT have been picked.
    assert "follow-up" not in rows[0]["title"]

    # The derived title is persisted back so a second load reads it from state
    # without re-fetching full history.
    full = asyncio.run(
        runner.session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=sid
        )
    )
    assert full.state.get(TITLE_KEY) == "FIRST original question"
