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

"""/lha/routines — list, user-scoped, over the shared routine store."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routines import attach_routines_routes
from app.scheduler.routine_store import (
    RoutineRow,
    get_routine_store,
    reset_routine_store,
)

pytestmark = pytest.mark.asyncio


def _routine(
    rid: str,
    user_id: str,
    next_fire_at: datetime,
    *,
    task: str = "do the daily thing",
    schedule: str = "0 9 * * *",
    secrets: tuple[str, ...] = (),
    delivery: str = "report_back",
) -> RoutineRow:
    return RoutineRow(
        id=rid,
        user_id=user_id,
        app_name="lha",
        schedule=schedule,
        task=task,
        secrets=secrets,
        delivery=delivery,
        next_fire_at=next_fire_at,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture()
def app_client(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "alice@x")
    monkeypatch.delenv("LHA_ROUTINE_STORE", raising=False)
    reset_routine_store()
    app = FastAPI()
    attach_routines_routes(app)
    yield TestClient(app)
    reset_routine_store()


async def test_list_returns_user_routines_sorted_by_next_fire_at(app_client):
    store = get_routine_store()
    await store.add(
        _routine("r2", "alice@x", datetime(2026, 6, 10, 9, tzinfo=UTC))
    )
    await store.add(
        _routine("r1", "alice@x", datetime(2026, 6, 5, 9, tzinfo=UTC))
    )
    body = app_client.get("/lha/routines").json()
    ids = [r["id"] for r in body["routines"]]
    assert ids == ["r1", "r2"]
    assert body["routines"][0]["next_fire_at"] == "2026-06-05T09:00:00+00:00"
    assert body["routines"][0]["task"] == "do the daily thing"
    assert body["routines"][0]["schedule"] == "0 9 * * *"


async def test_list_excludes_other_users(app_client):
    store = get_routine_store()
    await store.add(
        _routine("mine", "alice@x", datetime(2026, 6, 5, tzinfo=UTC))
    )
    await store.add(
        _routine("theirs", "bob@x", datetime(2026, 6, 5, tzinfo=UTC))
    )
    ids = [r["id"] for r in app_client.get("/lha/routines").json()["routines"]]
    assert ids == ["mine"]


async def test_delete_cancels_user_routine(app_client):
    store = get_routine_store()
    await store.add(
        _routine("mine", "alice@x", datetime(2026, 6, 5, tzinfo=UTC))
    )
    assert app_client.delete("/lha/routines/mine").status_code == 200
    assert app_client.get("/lha/routines").json()["routines"] == []


async def test_delete_missing_routine_404(app_client):
    assert app_client.delete("/lha/routines/nope").status_code == 404


async def test_delete_other_users_routine_404(app_client):
    store = get_routine_store()
    await store.add(
        _routine("theirs", "bob@x", datetime(2026, 6, 5, tzinfo=UTC))
    )
    assert app_client.delete("/lha/routines/theirs").status_code == 404


async def test_list_serializes_secrets_and_delivery(app_client):
    store = get_routine_store()
    await store.add(
        _routine(
            "r1",
            "alice@x",
            datetime(2026, 6, 5, 9, tzinfo=UTC),
            secrets=("GITHUB_PAT",),
            delivery="push",
        )
    )
    item = app_client.get("/lha/routines").json()["routines"][0]
    assert item["secrets"] == ["GITHUB_PAT"]
    assert item["delivery"] == "push"
