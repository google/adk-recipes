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

from horizon.environment import LocalEnvironment
from horizon.environment_context import set_active_environment
from horizon.routines import tools as routine_tools
from horizon.scheduler import routine_store as store_mod

pytestmark = pytest.mark.asyncio


class _TC:
    def __init__(self, confirmed=None, payload=None):
        self.confirmed = confirmed
        self.payload = payload


class _Ctx:
    def __init__(self, *, user_id="alice@x", tool_confirmation=None):
        self.state: dict = {}
        self.actions = SimpleNamespace(skip_summarization=False)
        self.tool_confirmation = tool_confirmation
        self._invocation_context = SimpleNamespace(
            user_id=user_id, app_name="app"
        )
        self._requested = None

    def request_confirmation(self, *, hint=None, payload=None):
        self._requested = {"hint": hint, "payload": payload}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    store_mod.reset_routine_store()
    monkeypatch.delenv("LHA_ROUTINE_STORE", raising=False)
    yield
    store_mod.reset_routine_store()


async def test_create_invalid_cron_rejected():
    ctx = _Ctx()
    res = await routine_tools.routine(
        action="create",
        name="x",
        schedule="not cron",
        task="t",
        tool_context=ctx,
    )
    assert res["success"] is False
    assert "schedule" in res["error"].lower()


async def test_create_first_call_requests_confirmation():
    ctx = _Ctx()
    res = await routine_tools.routine(
        action="create",
        name="Digest",
        schedule="0 8 * * *",
        task="summarize",
        secrets=["STRIPE_KEY"],
        tool_context=ctx,
    )
    assert res.get("status") == "awaiting_user_response"
    assert ctx._requested is not None  # HITL dispatched
    # nothing persisted yet
    assert await store_mod.get_routine_store().list_for_user("alice@x") == []


async def test_create_on_confirm_writes_and_registers(tmp_path):
    body = {
        "name": "Digest",
        "schedule": "0 8 * * *",
        "task": "summarize",
        "secrets": ["STRIPE_KEY"],
        "delivery": "report_back",
    }
    ctx = _Ctx(
        tool_confirmation=_TC(
            confirmed=True, payload={"manifest": body, "routine_id": "digest"}
        )
    )
    res = await routine_tools.routine(
        action="create",
        name="Digest",
        schedule="0 8 * * *",
        task="summarize",
        secrets=["STRIPE_KEY"],
        tool_context=ctx,
    )
    assert res["success"] is True and res["id"] == "digest"
    assert (tmp_path / ".lha/routines/digest.yaml").is_file()
    rows = await store_mod.get_routine_store().list_for_user("alice@x")
    assert [r.id for r in rows] == ["digest"]
    assert rows[0].secrets == ("STRIPE_KEY",)


async def test_create_declined_persists_nothing():
    ctx = _Ctx(tool_confirmation=_TC(confirmed=False, payload={}))
    res = await routine_tools.routine(
        action="create",
        name="Digest",
        schedule="0 8 * * *",
        task="t",
        tool_context=ctx,
    )
    assert res["success"] is False and res["status"] == "declined"
    assert await store_mod.get_routine_store().list_for_user("alice@x") == []


async def test_test_action_requires_task():
    ctx = _Ctx()
    res = await routine_tools.routine(action="test", name="x", tool_context=ctx)
    assert res["success"] is False and "task" in res["error"].lower()


async def test_test_action_dispatches_without_persisting(monkeypatch):
    captured = {}

    async def fake_run_once(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "status": "completed",
            "output": "ran",
            "iterations": 2,
            "duration_ms": 7,
        }

    monkeypatch.setattr(routine_tools, "run_routine_once", fake_run_once)
    ctx = _Ctx()
    res = await routine_tools.routine(
        action="test",
        name="Daily Digest",
        task="summarize",
        secrets=["STRIPE_KEY"],
        tool_context=ctx,
    )
    assert res["status"] == "completed" and res["output"] == "ran"
    assert captured["routine_id"] == "daily-digest"
    assert captured["owner"] == "alice@x"
    assert captured["task"] == "summarize"
    assert list(captured["secrets"]) == ["STRIPE_KEY"]
    # a test run persists nothing
    assert await store_mod.get_routine_store().list_for_user("alice@x") == []


async def test_list_and_cancel():
    body = {"name": "D", "schedule": "0 8 * * *", "task": "t"}
    ctx = _Ctx(
        tool_confirmation=_TC(
            confirmed=True, payload={"manifest": body, "routine_id": "d"}
        )
    )
    await routine_tools.routine(
        action="create",
        name="D",
        schedule="0 8 * * *",
        task="t",
        tool_context=ctx,
    )

    listed = await routine_tools.routine(action="list", tool_context=_Ctx())
    assert listed["success"] is True and len(listed["routines"]) == 1

    cancelled = await routine_tools.routine(
        action="cancel", id="d", tool_context=_Ctx()
    )
    assert cancelled["success"] is True
    assert (await routine_tools.routine(action="list", tool_context=_Ctx()))[
        "routines"
    ] == []


async def test_cancel_deletes_manifest_file(tmp_path):
    body = {
        "name": "Digest",
        "schedule": "0 8 * * *",
        "task": "summarize",
        "secrets": [],
        "delivery": "report_back",
    }
    ctx = _Ctx(
        user_id="alice@x",
        tool_confirmation=_TC(
            confirmed=True, payload={"manifest": body, "routine_id": "digest"}
        ),
    )
    res = await routine_tools.routine(
        action="create",
        name="Digest",
        schedule="0 8 * * *",
        task="summarize",
        tool_context=ctx,
    )
    assert res["success"] is True
    manifest_path = tmp_path / ".lha/routines/digest.yaml"
    assert manifest_path.is_file()

    cancelled = await routine_tools.routine(
        action="cancel", id="digest", tool_context=_Ctx(user_id="alice@x")
    )
    assert cancelled["success"] is True
    assert not manifest_path.exists()


async def test_cancel_wrong_user_leaves_manifest_file(tmp_path):
    body = {
        "name": "Digest",
        "schedule": "0 8 * * *",
        "task": "summarize",
        "secrets": [],
        "delivery": "report_back",
    }
    ctx = _Ctx(
        user_id="alice@x",
        tool_confirmation=_TC(
            confirmed=True, payload={"manifest": body, "routine_id": "digest"}
        ),
    )
    res = await routine_tools.routine(
        action="create",
        name="Digest",
        schedule="0 8 * * *",
        task="summarize",
        tool_context=ctx,
    )
    assert res["success"] is True
    manifest_path = tmp_path / ".lha/routines/digest.yaml"
    assert manifest_path.is_file()

    # Bob tries to cancel Alice's routine
    cancelled = await routine_tools.routine(
        action="cancel", id="digest", tool_context=_Ctx(user_id="bob@x")
    )
    assert cancelled["success"] is False
    assert manifest_path.is_file()


async def test_cancel_missing_manifest_still_succeeds():
    store = store_mod.get_routine_store()
    now = datetime.now(UTC)
    await store.add(
        store_mod.RoutineRow(
            id="orphan",
            user_id="alice@x",
            app_name="app",
            schedule="0 8 * * *",
            task="t",
            secrets=(),
            delivery="report_back",
            created_at=now,
            next_fire_at=now,
        )
    )
    cancelled = await routine_tools.routine(
        action="cancel", id="orphan", tool_context=_Ctx(user_id="alice@x")
    )
    assert cancelled["success"] is True
