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

"""Async repository for per-user settings (`user_settings` table)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.store.engine import SqlStore
from app.store.models import UserSettingRecord


class UserSettingsStore(SqlStore):
    async def load(self, principal_id: str) -> dict[str, Any]:
        factory = await self._ready()
        async with factory() as session:
            rows = await session.execute(
                select(
                    UserSettingRecord.key, UserSettingRecord.value_json
                ).where(UserSettingRecord.principal_id == principal_id)
            )
            return {
                key: value for key, value in rows.all() if value is not None
            }

    async def load_key(self, key: str) -> dict[str, Any]:
        factory = await self._ready()
        async with factory() as session:
            rows = await session.execute(
                select(
                    UserSettingRecord.principal_id, UserSettingRecord.value_json
                ).where(UserSettingRecord.key == key)
            )
            return {
                principal: value
                for principal, value in rows.all()
                if value is not None
            }

    async def save(
        self, principal_id: str, updates: Mapping[str, Any]
    ) -> dict[str, Any]:
        factory = await self._ready()
        async with factory() as session:
            for key, value in updates.items():
                if value is None:
                    await session.execute(
                        delete(UserSettingRecord).where(
                            UserSettingRecord.principal_id == principal_id,
                            UserSettingRecord.key == key,
                        )
                    )
                    continue
                await session.execute(self._upsert(principal_id, key, value))
            await session.commit()
        return await self.load(principal_id)

    def _upsert(self, principal_id: str, key: str, value: Any) -> Any:
        insert = (
            pg_insert
            if self._engine.dialect.name == "postgresql"
            else sqlite_insert
        )
        stmt = insert(UserSettingRecord).values(
            principal_id=principal_id, key=key, value_json=value
        )
        return stmt.on_conflict_do_update(
            index_elements=[
                UserSettingRecord.principal_id,
                UserSettingRecord.key,
            ],
            set_={
                "value_json": stmt.excluded.value_json,
                "updated_at": func.now(),
            },
        )


_store: UserSettingsStore | None = None


def get_user_settings_store() -> UserSettingsStore:
    global _store
    if _store is None:
        _store = UserSettingsStore()
    return _store


def set_user_settings_store(store: UserSettingsStore | None) -> None:
    global _store
    _store = store
