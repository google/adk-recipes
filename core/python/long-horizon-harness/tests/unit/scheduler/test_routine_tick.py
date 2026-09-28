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

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.identity import get_user_id_from_context
from app.guardrails.permission_guard import _HEADLESS
from app.routines.run_context import active_routine_run
from app.scheduler import routine_store as store_mod
from app.scheduler import routine_tick_endpoint
from app.scheduler.routine_store import RoutineRow
from app.secrets.inject import _routine_secret_scope

try:
    from google.adk.sessions import InMemorySessionService
except Exception:  # pragma: no cover
    InMemorySessionService = None

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    store_mod.reset_routine_store()
    monkeypatch.delenv("LHA_ROUTINE_STORE", raising=False)
    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    yield
    store_mod.reset_routine_store()


async def test_routine_tick_runs_turn_with_routine_context(monkeypatch):
    captured = {}

    async def fake_turn(handler, *, session_id, routine):
        # the fire path must install all three routine ContextVars before the turn
        captured["routine_run"] = active_routine_run()
        captured["headless"] = _HEADLESS.get()
        captured["scope"] = _routine_secret_scope.get()
        captured["user"] = get_user_id_from_context()
        return "done"

    monkeypatch.setattr(routine_tick_endpoint, "_run_routine_turn", fake_turn)

    store = store_mod.get_routine_store()
    await store.add(
        RoutineRow(
            id="digest",
            user_id="alice@x",
            app_name="app",
            schedule="0 8 * * *",
            task="summarize",
            secrets=("STRIPE_KEY",),
            delivery="report_back",
            next_fire_at=datetime(2026, 1, 1, tzinfo=UTC),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )

    app = FastAPI()
    app.include_router(routine_tick_endpoint.router)
    app.state.runner = SimpleNamespace(
        session_service=InMemorySessionService(), app_name="app"
    )
    app.state.a2a_handler = object()

    resp = TestClient(app).post("/scheduler/routine-tick")
    assert resp.status_code == 200
    assert resp.json()["fired"] == 1
    # the three ContextVars were active during the turn, reset after
    assert captured["routine_run"].routine_id == "digest"
    assert captured["headless"] is True
    assert captured["scope"] == frozenset({"STRIPE_KEY"})
    assert captured["user"] == "alice@x"
    assert active_routine_run() is None
    assert _HEADLESS.get() is False
