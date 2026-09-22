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

"""Routine schedule rows + store.

The row carries the manifest fields (task/secrets/delivery/schedule) so the fire
path is self-contained — it never reads the user's `.lha/routines/` at fire time
(a routine runs in a fresh sandbox that lacks the user's workspace). `.lha/` holds
the human-readable source; this row is the runtime copy.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from horizon.scheduler.cron import next_cron_fire


@dataclass(frozen=True)
class RoutineRow:
    id: str
    user_id: str
    app_name: str
    schedule: str
    task: str
    secrets: tuple[str, ...]
    delivery: str
    next_fire_at: datetime
    created_at: datetime


class RoutineStore(Protocol):
    async def add(self, row: RoutineRow) -> str: ...
    async def claim_due(self, cursor: datetime) -> list[RoutineRow]: ...
    async def list_for_user(self, user_id: str) -> list[RoutineRow]: ...
    async def cancel(self, routine_id: str, user_id: str) -> bool: ...


class InMemoryRoutineStore:
    def __init__(self) -> None:
        self._rows: dict[str, RoutineRow] = {}
        self._lock = asyncio.Lock()

    async def add(self, row: RoutineRow) -> str:
        rid = row.id or uuid.uuid4().hex
        async with self._lock:
            existing = self._rows.get(rid)
            # id is a global slug; an upsert lets the owner edit a routine in
            # place, but never lets another user clobber one they don't own.
            if existing is None or existing.user_id == row.user_id:
                self._rows[rid] = replace(row, id=rid)
        return rid

    async def claim_due(self, cursor: datetime) -> list[RoutineRow]:
        claimed: list[RoutineRow] = []
        async with self._lock:
            for rid, row in list(self._rows.items()):
                if row.next_fire_at <= cursor:
                    claimed.append(row)
                    try:
                        nxt = next_cron_fire(row.schedule, cursor)
                    except ValueError:
                        # unparseable schedule → drop the row rather than loop
                        del self._rows[rid]
                        continue
                    self._rows[rid] = replace(row, next_fire_at=nxt)
        return claimed

    async def list_for_user(self, user_id: str) -> list[RoutineRow]:
        async with self._lock:
            return sorted(
                (r for r in self._rows.values() if r.user_id == user_id),
                key=lambda r: r.next_fire_at,
            )

    async def cancel(self, routine_id: str, user_id: str) -> bool:
        async with self._lock:
            row = self._rows.get(routine_id)
            if row is None or row.user_id != user_id:
                return False
            del self._rows[routine_id]
            return True


_STORE_ENV = "LHA_ROUTINE_STORE"
_singleton: RoutineStore | None = None


def get_routine_store() -> RoutineStore:
    global _singleton
    if _singleton is not None:
        return _singleton
    backend = os.environ.get(_STORE_ENV, "memory").strip().lower()
    if backend == "postgres":
        from horizon.scheduler.routine_postgres_store import build_from_env

        _singleton = build_from_env()
    elif backend == "memory":
        _singleton = InMemoryRoutineStore()
    else:
        raise ValueError(
            f"unknown {_STORE_ENV}={backend!r}; expected 'memory' or 'postgres'"
        )
    return _singleton


def reset_routine_store() -> None:
    global _singleton
    _singleton = None


def active_routine_store() -> RoutineStore | None:
    """Return the cached singleton, or None if it has not been built yet.

    Lets a shutdown hook close a Postgres pool only when one was opened,
    without spinning up a store just to tear it down.
    """
    return _singleton


__all__ = [
    "InMemoryRoutineStore",
    "RoutineRow",
    "RoutineStore",
    "active_routine_store",
    "get_routine_store",
    "reset_routine_store",
]
