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

"""Unit tests for /lha/sessions source/jobType fields and ?source=/?q= filters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from horizon.api.sessions import TITLE_KEY, attach_session_routes
from horizon.auth import current_user_id
from horizon.infrastructure.constants import APP_NAME
from horizon.scheduler.sessions import create_scheduled_session

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


async def _seed_user_chat(runner: _StubRunner, title: str) -> str:
    s = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, state={TITLE_KEY: title}
    )
    return s.id


async def _seed_scheduled(
    runner: _StubRunner, job_type: str, title: str
) -> str:
    s = await create_scheduled_session(
        runner.session_service,
        app_name=APP_NAME,
        user_id=USER_ID,
        job_type=job_type,
        title=title,
    )
    return s.id


def test_default_list_includes_source_and_jobtype_fields() -> None:
    runner = _StubRunner()
    asyncio.run(_seed_user_chat(runner, "hello chat"))
    asyncio.run(_seed_scheduled(runner, "routine", "Routine: standup"))

    client = TestClient(_build_app(runner))
    rows = client.get("/lha/sessions").json()
    by_title = {r["title"]: r for r in rows}
    assert by_title["hello chat"]["source"] == "user"
    assert by_title["hello chat"]["jobType"] is None
    assert by_title["Routine: standup"]["source"] == "scheduler"
    assert by_title["Routine: standup"]["jobType"] == "routine"


def test_source_user_excludes_scheduler() -> None:
    runner = _StubRunner()
    asyncio.run(_seed_user_chat(runner, "hello chat"))
    asyncio.run(_seed_scheduled(runner, "dream_review", "Dream review — x"))

    client = TestClient(_build_app(runner))
    rows = client.get("/lha/sessions?source=user").json()
    titles = {r["title"] for r in rows}
    assert "hello chat" in titles
    assert "Dream review — x" not in titles


def test_source_scheduler_only_returns_scheduled() -> None:
    runner = _StubRunner()
    asyncio.run(_seed_user_chat(runner, "hello chat"))
    asyncio.run(_seed_scheduled(runner, "routine", "Routine: standup"))
    asyncio.run(_seed_scheduled(runner, "dream_review", "Dream review — x"))

    client = TestClient(_build_app(runner))
    rows = client.get("/lha/sessions?source=scheduler").json()
    titles = {r["title"] for r in rows}
    assert titles == {"Routine: standup", "Dream review — x"}


def test_q_filters_by_title_case_insensitive() -> None:
    runner = _StubRunner()
    asyncio.run(_seed_scheduled(runner, "routine", "Routine: Standup"))
    asyncio.run(_seed_scheduled(runner, "routine", "Routine: lunch"))

    client = TestClient(_build_app(runner))
    rows = client.get("/lha/sessions?source=scheduler&q=stand").json()
    assert [r["title"] for r in rows] == ["Routine: Standup"]
