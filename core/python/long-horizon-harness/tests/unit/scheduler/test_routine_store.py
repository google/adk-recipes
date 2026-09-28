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

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.scheduler.routine_store import (
    InMemoryRoutineStore,
    RoutineRow,
    get_routine_store,
    reset_routine_store,
)

pytestmark = pytest.mark.asyncio


def _row(rid="digest", next_fire_at=None):
    return RoutineRow(
        id=rid,
        user_id="alice@x",
        app_name="app",
        schedule="0 8 * * *",
        task="summarize",
        secrets=("STRIPE_KEY",),
        delivery="report_back",
        next_fire_at=next_fire_at or datetime(2026, 6, 14, 8, 0, tzinfo=UTC),
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def test_add_and_list():
    store = InMemoryRoutineStore()
    await store.add(_row())
    rows = await store.list_for_user("alice@x")
    assert [r.id for r in rows] == ["digest"]


async def test_claim_due_advances_next_fire():
    store = InMemoryRoutineStore()
    await store.add(_row(next_fire_at=datetime(2026, 6, 14, 8, 0, tzinfo=UTC)))
    cursor = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    claimed = await store.claim_due(cursor)
    assert [r.id for r in claimed] == ["digest"]
    # next_fire advanced to the next 08:00 (strictly after the cursor)
    rows = await store.list_for_user("alice@x")
    assert rows[0].next_fire_at == datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


async def test_claim_due_skips_future():
    store = InMemoryRoutineStore()
    await store.add(_row(next_fire_at=datetime(2026, 6, 20, 8, 0, tzinfo=UTC)))
    assert await store.claim_due(datetime(2026, 6, 14, 9, 0, tzinfo=UTC)) == []


async def test_add_same_id_same_user_updates_in_place():
    store = InMemoryRoutineStore()
    await store.add(_row(rid="digest"))
    await store.add(
        replace(_row(rid="digest"), schedule="0 9 * * *", task="new task")
    )
    rows = await store.list_for_user("alice@x")
    assert len(rows) == 1
    assert rows[0].task == "new task"
    assert rows[0].schedule == "0 9 * * *"


async def test_add_other_user_cannot_clobber():
    store = InMemoryRoutineStore()
    await store.add(_row(rid="digest"))  # owned by alice@x
    await store.add(
        replace(_row(rid="digest"), user_id="bob@x", task="bob task")
    )
    alice_rows = await store.list_for_user("alice@x")
    assert len(alice_rows) == 1
    assert alice_rows[0].task == "summarize"  # alice's row untouched
    assert await store.list_for_user("bob@x") == []


async def test_cancel():
    store = InMemoryRoutineStore()
    await store.add(_row())
    assert await store.cancel("digest", "alice@x") is True
    assert await store.list_for_user("alice@x") == []
    assert await store.cancel("digest", "alice@x") is False


async def test_get_store_singleton_and_reset(monkeypatch):
    monkeypatch.delenv("LHA_ROUTINE_STORE", raising=False)
    reset_routine_store()
    s1 = get_routine_store()
    s2 = get_routine_store()
    assert s1 is s2
    reset_routine_store()
    assert get_routine_store() is not s1
