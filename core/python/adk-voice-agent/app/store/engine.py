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

"""Database engine and session lifecycle management for ADK Voice Agent."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.app_utils.env_flags import env_flag
from app.store.models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None


def get_default_state_dir() -> Path:
    custom = os.getenv("VOICE_AGENT_STATE_DIR")
    if custom:
        p = Path(custom).expanduser().resolve()
    else:
        p = Path.home() / ".adk-voice-agent"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_database_url() -> str:
    for key in ("THREAD_DB_URL", "DATABASE_URL", "SESSION_DB_URL"):
        val = os.getenv(key)
        if val and val.strip():
            url = val.strip()
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif url.startswith("sqlite://") and not url.startswith(
                "sqlite+aiosqlite://"
            ):
                url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
            return url

    db_path = get_default_state_dir() / "threads.db"
    return f"sqlite+aiosqlite:///{db_path}"


def get_async_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = resolve_database_url()
        is_postgres = "postgresql" in url

        engine_kwargs: dict[str, Any] = {
            "echo": env_flag("SQL_DEBUG"),
        }

        if is_postgres:
            engine_kwargs.update(
                {
                    "pool_size": int(os.getenv("DB_POOL_SIZE") or "2"),
                    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW") or "3"),
                    "pool_pre_ping": True,
                    "pool_recycle": 300,
                }
            )

        _engine = create_async_engine(url, **engine_kwargs)
        logger.info(
            "Initialized database engine with URL schema: %s",
            url.split("://")[0],
        )

    return _engine


async def init_db(engine: AsyncEngine | None = None) -> None:
    engine = engine or get_async_engine()

    if engine.dialect.name == "sqlite":
        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema initialized.")


class SqlStore:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine or get_async_engine()
        self._session_factory = async_sessionmaker(
            bind=self._engine, expire_on_commit=False
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ready(self) -> async_sessionmaker[AsyncSession]:
        if not self._initialized:
            async with self._init_lock:
                if not self._initialized:
                    await init_db(self._engine)
                    self._initialized = True
        return self._session_factory


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()
