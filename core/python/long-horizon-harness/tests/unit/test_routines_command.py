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

"""The /routines slash command lists scheduled routines and removes them."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.auth.identity import user_identity_scope
from app.commands import BUILTIN_COMMAND_REGISTRY
from app.scheduler.routine_store import (
    InMemoryRoutineStore,
    RoutineRow,
    reset_routine_store,
)

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self):
        self.state: dict = {}


async def test_routines_lists_empty():
    reset_routine_store()
    handler = BUILTIN_COMMAND_REGISTRY["routines"]
    ctx = _Ctx()
    with user_identity_scope("alice@local"):
        out = await handler("", ctx)
    assert "no" in out.lower()


async def test_routines_lists_and_removes():
    reset_routine_store()
    handler = BUILTIN_COMMAND_REGISTRY["routines"]

    # Seed the store with a routine for alice
    store = InMemoryRoutineStore()
    row = RoutineRow(
        id="digest",
        user_id="alice@local",
        app_name="test-app",
        schedule="0 8 * * *",
        task="summarize and report",
        secrets=("STRIPE_KEY",),
        delivery="report_back",
        next_fire_at=datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
        created_at=datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    )
    await store.add(row)

    # Inject the store as the singleton so the command finds it
    import app.scheduler.routine_store as store_mod

    store_mod._singleton = store

    ctx = _Ctx()

    # Test list shows the routine
    with user_identity_scope("alice@local"):
        listed = await handler("", ctx)
    assert "digest" in listed
    assert "0 8 * * *" in listed

    # Test remove
    with user_identity_scope("alice@local"):
        removed = await handler("remove digest", ctx)
    assert "removed" in removed.lower()

    # Test list now shows none
    with user_identity_scope("alice@local"):
        empty = await handler("", ctx)
    assert "no" in empty.lower()
