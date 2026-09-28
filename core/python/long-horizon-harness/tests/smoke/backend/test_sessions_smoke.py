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

"""HTTP-level: GET /lha/sessions?source=user returns well-formed rows quickly,
even when a session carries many events. Guards against the O(events) regression
that produced 502s at the proxy. Gated by RUN_SMOKE=1.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.api.sessions import TITLE_KEY, attach_session_routes
from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SMOKE") != "1", reason="RUN_SMOKE=1 required"
)

USER_ID = "smoke-sessions@local"


@dataclass
class _Runner:
    session_service: InMemorySessionService


def _user_event(text: str) -> Event:
    return Event(
        invocation_id="inv",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text=text)]),
    )


@pytest_asyncio.fixture
async def smoke_client() -> TestClient:
    svc = InMemorySessionService()
    session = await svc.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={TITLE_KEY: "heavy chat"}
    )
    # A few hundred trivial events — the regression this guards against loaded
    # and deserialized every one of these per row.
    for i in range(300):
        await svc.append_event(session, _user_event(f"msg {i}"))

    app = FastAPI()
    attach_session_routes(app, runner=_Runner(session_service=svc))
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return TestClient(app)


def test_sessions_user_list_is_fast_and_wellformed(
    smoke_client: TestClient,
) -> None:
    start = time.monotonic()
    resp = smoke_client.get("/lha/sessions?source=user")
    elapsed = time.monotonic() - start

    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    for r in rows:
        assert set(r) >= {
            "id",
            "title",
            "createdAt",
            "lastUpdated",
            "source",
            "jobType",
        }
        assert r["source"] in {"user", "scheduler"}
    # Generous ceiling: the point is it does not approach the 60s proxy timeout.
    assert elapsed < 10.0, f"sessions list too slow: {elapsed:.2f}s"
