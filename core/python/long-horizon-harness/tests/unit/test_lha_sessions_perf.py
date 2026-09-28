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

"""GET /lha/sessions must derive title/source/timestamps WITHOUT loading the
full event history of each session. We assert get_session is called with a
bounded GetSessionConfig (num_recent_events set), so a heavy user's list stays
O(sessions) not O(sessions x events).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.base_session_service import GetSessionConfig

from app.api.sessions import TITLE_KEY, attach_session_routes
from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME

USER_ID = "u@local"


@dataclass
class _RecordingSessionService:
    inner: InMemorySessionService = field(
        default_factory=InMemorySessionService
    )
    get_calls: list[object] = field(default_factory=list)

    async def list_sessions(self, **kw):
        return await self.inner.list_sessions(**kw)

    async def get_session(self, *, config=None, **kw):
        self.get_calls.append(config)
        return await self.inner.get_session(config=config, **kw)

    async def create_session(self, **kw):
        return await self.inner.create_session(**kw)


@dataclass
class _StubRunner:
    session_service: _RecordingSessionService = field(
        default_factory=_RecordingSessionService
    )


def _build_app(runner: _StubRunner) -> FastAPI:
    app = FastAPI()
    attach_session_routes(app, runner=runner)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


def test_list_passes_bounded_config_to_get_session() -> None:
    runner = _StubRunner()
    asyncio.run(
        runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, state={TITLE_KEY: "hello"}
        )
    )
    client = TestClient(_build_app(runner))

    rows = client.get("/lha/sessions?source=user").json()

    assert any(r["title"] == "hello" for r in rows)
    assert runner.session_service.get_calls, "get_session was never called"
    for cfg in runner.session_service.get_calls:
        assert isinstance(cfg, GetSessionConfig), (
            "get_session called without a config"
        )
        assert cfg.num_recent_events is not None, (
            "config did not bound event load"
        )
