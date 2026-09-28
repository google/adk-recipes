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

"""Unit tests for ``DELETE /lha/sessions`` (bulk delete).

Lists sessions for the current user, calls ``delete_session`` for each,
returns 204 with a JSON body reporting how many were removed. Failures
for individual sessions don't abort the batch — they're collected and
the response surfaces a non-zero ``failed`` count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.api.sessions import attach_session_routes
from app.auth import current_user_id
from app.infrastructure.constants import APP_NAME

USER_ID = "u@local"
OTHER_USER = "other@local"


def _build_app(runner: Runner) -> FastAPI:
    app = FastAPI()
    attach_session_routes(app, runner=runner)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return app


@dataclass
class _StubRunner:
    """Minimal Runner stand-in carrying just the session_service handle
    the routes touch. The real Runner needs a populated agent + artifact
    service which we don't exercise here."""

    session_service: InMemorySessionService = field(
        default_factory=InMemorySessionService
    )


@pytest.fixture
def runner() -> _StubRunner:
    return _StubRunner()


@pytest.mark.asyncio
async def _seed(runner: _StubRunner, user_id: str, n: int) -> list[str]:
    ids: list[str] = []
    for _ in range(n):
        s = await runner.session_service.create_session(
            app_name=APP_NAME, user_id=user_id
        )
        ids.append(s.id)
    return ids


def test_delete_all_removes_only_current_users_sessions(
    runner: _StubRunner,
) -> None:
    import asyncio

    asyncio.run(_seed(runner, USER_ID, 3))
    asyncio.run(_seed(runner, OTHER_USER, 2))

    client = TestClient(_build_app(runner))
    resp = client.delete("/lha/sessions")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"deleted": 3, "failed": 0}

    remaining_for_user = asyncio.run(
        runner.session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID)
    ).sessions
    remaining_for_other = asyncio.run(
        runner.session_service.list_sessions(
            app_name=APP_NAME, user_id=OTHER_USER
        )
    ).sessions
    assert remaining_for_user == []
    assert len(remaining_for_other) == 2


def test_delete_all_with_no_sessions_is_a_noop(runner: _StubRunner) -> None:
    client = TestClient(_build_app(runner))
    resp = client.delete("/lha/sessions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 0, "failed": 0}


def test_delete_all_continues_past_individual_failures(
    runner: _StubRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    ids = asyncio.run(_seed(runner, USER_ID, 3))
    bad_id = ids[1]

    real_delete = runner.session_service.delete_session

    async def flaky_delete(
        *, app_name: str, user_id: str, session_id: str
    ) -> None:
        if session_id == bad_id:
            raise RuntimeError("simulated backend failure")
        await real_delete(
            app_name=app_name, user_id=user_id, session_id=session_id
        )

    monkeypatch.setattr(runner.session_service, "delete_session", flaky_delete)

    client = TestClient(_build_app(runner))
    resp = client.delete("/lha/sessions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2, "failed": 1}

    remaining = asyncio.run(
        runner.session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID)
    ).sessions
    assert [s.id for s in remaining] == [bad_id]
