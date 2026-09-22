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

"""``PostgresRoutineStore`` — asyncpg-backed implementation of the
:class:`~horizon.scheduler.routine_store.RoutineStore` protocol.

Connection string is read from ``LHA_REMINDER_DB_URL``. The name is
historical: it predates the reminder capability's removal and is now owned
solely by routines, kept as-is to avoid a Terraform/env rename for no user
benefit. The schema bootstrap (``CREATE TABLE IF NOT EXISTS``) runs once per
process on first pool use — idempotent and safe across Cloud Run replicas
because Postgres serializes the DDL.

The ``secrets`` tuple is stored as JSON text; ``next_fire_at`` advances on claim
via ``next_cron_fire`` (routines recur — they are never deleted on claim).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

import asyncpg

from horizon.infrastructure.db_resilience import retry_on_disconnect
from horizon.scheduler.cron import next_cron_fire
from horizon.scheduler.routine_store import RoutineRow

logger = logging.getLogger(__name__)

# Recycle idle asyncpg connections well under Cloud SQL's idle timeout so the
# every-minute tick never hands out a connection the server already reaped.
_MAX_INACTIVE_LIFETIME_SECONDS = 300.0
_CONNECT_TIMEOUT_SECONDS = 10.0


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routines (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    app_name      TEXT NOT NULL,
    schedule      TEXT NOT NULL,
    task          TEXT NOT NULL,
    secrets       TEXT NOT NULL,
    delivery      TEXT NOT NULL,
    next_fire_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS routines_next_fire_at_idx ON routines (next_fire_at);
CREATE INDEX IF NOT EXISTS routines_user_id_idx ON routines (user_id);
"""


# Upsert by id so re-creating a routine (same slug) edits it in place instead
# of failing on the primary key. WHERE-guarded on user_id so a shared slug can
# never clobber another user's routine; created_at/user_id are left untouched.
_INSERT_SQL = """
INSERT INTO routines (
    id, user_id, app_name, schedule, task,
    secrets, delivery, next_fire_at, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (id) DO UPDATE SET
    app_name = EXCLUDED.app_name,
    schedule = EXCLUDED.schedule,
    task = EXCLUDED.task,
    secrets = EXCLUDED.secrets,
    delivery = EXCLUDED.delivery,
    next_fire_at = EXCLUDED.next_fire_at
WHERE routines.user_id = EXCLUDED.user_id
"""

# FOR UPDATE SKIP LOCKED so two concurrent ticks claim disjoint rows.
_CLAIM_DUE_SQL = """
SELECT id, user_id, app_name, schedule, task,
       secrets, delivery, next_fire_at, created_at
FROM routines
WHERE next_fire_at <= $1
ORDER BY next_fire_at ASC
FOR UPDATE SKIP LOCKED
"""

_SELECT_USER_SQL = """
SELECT id, user_id, app_name, schedule, task,
       secrets, delivery, next_fire_at, created_at
FROM routines
WHERE user_id = $1
ORDER BY next_fire_at ASC
"""

_DELETE_SQL = "DELETE FROM routines WHERE id = $1"
_UPDATE_FIRE_AT_SQL = "UPDATE routines SET next_fire_at = $1 WHERE id = $2"
_CANCEL_SQL = "DELETE FROM routines WHERE id = $1 AND user_id = $2"


class PostgresRoutineStore:
    """asyncpg-backed routine store. Pool + schema initialized lazily."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgresRoutineStore requires a non-empty DSN")
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._schema_ready = False
        self._init_lock = asyncio.Lock()
        self._retry_base_delay = 0.25

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is not None and self._schema_ready:
            return self._pool
        async with self._init_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=1,
                    # Bounded to stay within the per-instance connection budget
                    # (see db_resilience): the scheduler is low-QPS.
                    max_size=3,
                    max_inactive_connection_lifetime=_MAX_INACTIVE_LIFETIME_SECONDS,
                    timeout=_CONNECT_TIMEOUT_SECONDS,
                )
            if not self._schema_ready:
                await self._pool.execute(_SCHEMA_SQL)
                self._schema_ready = True
            return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    async def add(self, row: RoutineRow) -> str:
        pool = await self._ensure_pool()
        rid = row.id or uuid.uuid4().hex
        await retry_on_disconnect(
            lambda: pool.execute(
                _INSERT_SQL,
                rid,
                row.user_id,
                row.app_name,
                row.schedule,
                row.task,
                json.dumps(list(row.secrets)),
                row.delivery,
                row.next_fire_at,
                row.created_at,
            ),
            base_delay=self._retry_base_delay,
        )
        return rid

    async def claim_due(self, cursor: Any) -> list[RoutineRow]:
        pool = await self._ensure_pool()

        # Wrap the whole acquire/transaction so a mid-claim drop re-runs the
        # entire claim atomically — a partial claim must not be retried piecemeal.
        async def op() -> list[RoutineRow]:
            claimed: list[RoutineRow] = []
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await conn.fetch(_CLAIM_DUE_SQL, cursor)
                    for row in rows:
                        try:
                            next_at = next_cron_fire(row["schedule"], cursor)
                        except ValueError:
                            logger.exception(
                                "claim_due: invalid cron %r on routine %s; dropping",
                                row["schedule"],
                                row["id"],
                            )
                            await conn.execute(_DELETE_SQL, row["id"])
                            continue
                        await conn.execute(
                            _UPDATE_FIRE_AT_SQL, next_at, row["id"]
                        )
                        claimed.append(_row_to_routine(row))
            return claimed

        return await retry_on_disconnect(op, base_delay=self._retry_base_delay)

    async def list_for_user(self, user_id: str) -> list[RoutineRow]:
        pool = await self._ensure_pool()
        rows = await retry_on_disconnect(
            lambda: pool.fetch(_SELECT_USER_SQL, user_id),
            base_delay=self._retry_base_delay,
        )
        return [_row_to_routine(r) for r in rows]

    async def cancel(self, routine_id: str, user_id: str) -> bool:
        pool = await self._ensure_pool()
        status = await retry_on_disconnect(
            lambda: pool.execute(_CANCEL_SQL, routine_id, user_id),
            base_delay=self._retry_base_delay,
        )
        # asyncpg returns "DELETE N" — N==0 means no row matched
        return status.upper().startswith("DELETE ") and not status.endswith(
            " 0"
        )


def _row_to_routine(row: Any) -> RoutineRow:
    return RoutineRow(
        id=row["id"],
        user_id=row["user_id"],
        app_name=row["app_name"],
        schedule=row["schedule"],
        task=row["task"],
        secrets=tuple(json.loads(row["secrets"])),
        delivery=row["delivery"],
        next_fire_at=row["next_fire_at"],
        created_at=row["created_at"],
    )


def build_from_env() -> PostgresRoutineStore:
    """Construct a store from ``LHA_REMINDER_DB_URL`` (historical name, now
    owned solely by routines; see the module docstring).

    Raises ``ValueError`` if the env var is unset — surfaces config gaps
    at process startup instead of at first routine write.
    """
    dsn = os.environ.get("LHA_REMINDER_DB_URL", "").strip()
    if not dsn:
        raise ValueError(
            "LHA_ROUTINE_STORE=postgres requires LHA_REMINDER_DB_URL "
            "(e.g. postgres://user@/db?host=/cloudsql/<conn_name>)"
        )
    return PostgresRoutineStore(dsn=dsn)


__all__ = ["PostgresRoutineStore", "build_from_env"]
