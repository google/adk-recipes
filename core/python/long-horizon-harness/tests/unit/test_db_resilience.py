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

"""Connection-resilience defaults for the SQL-backed session/task stores."""

from __future__ import annotations

import asyncpg
import pytest

from app.infrastructure.db_resilience import (
    is_pooled_sql_url,
    resilient_engine_kwargs,
)


def test_resilient_engine_kwargs_enable_pre_ping_and_recycle():
    kwargs = resilient_engine_kwargs()
    assert kwargs["pool_pre_ping"] is True
    assert isinstance(kwargs["pool_recycle"], int)
    assert 0 < kwargs["pool_recycle"] <= 3600


def test_resilient_engine_kwargs_is_a_fresh_dict_each_call():
    a = resilient_engine_kwargs()
    a["pool_pre_ping"] = False
    assert resilient_engine_kwargs()["pool_pre_ping"] is True


def test_resilient_engine_kwargs_bounds_pool_when_pooled():
    # Per-instance connection budget: bounded so (cap x max-instances) stays
    # under Cloud SQL max_connections.
    kwargs = resilient_engine_kwargs()
    assert kwargs["pool_size"] == 4
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_timeout"] == 30


def test_resilient_engine_kwargs_omits_pool_sizing_when_not_pooled():
    # sqlite uses StaticPool/NullPool, which reject QueuePool-only args.
    kwargs = resilient_engine_kwargs(pooled=False)
    assert "pool_size" not in kwargs
    assert "max_overflow" not in kwargs
    assert "pool_timeout" not in kwargs
    # The always-safe resilience knobs stay regardless of pool type.
    assert kwargs["pool_pre_ping"] is True
    assert "pool_recycle" in kwargs
    assert kwargs["connect_args"]["timeout"] == 10


def test_resilient_engine_kwargs_pool_size_env_override(monkeypatch):
    monkeypatch.setenv("LHA_DB_POOL_SIZE", "7")
    monkeypatch.setenv("LHA_DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("LHA_DB_POOL_TIMEOUT", "15")
    kwargs = resilient_engine_kwargs()
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 2
    assert kwargs["pool_timeout"] == 15


def test_resilient_engine_kwargs_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LHA_DB_POOL_SIZE", "not-an-int")
    assert resilient_engine_kwargs()["pool_size"] == 4


def test_is_pooled_sql_url_accepts_server_based_dialects():
    assert is_pooled_sql_url("postgresql+asyncpg://u:p@/db?host=/cloudsql/x")
    assert is_pooled_sql_url("postgres://u:p@host/db")


def test_is_pooled_sql_url_rejects_backends_that_drop_engine_kwargs():
    # sqlite routes to SqliteSessionService (kwargs ignored + warns); the others
    # aren't SQLAlchemy engines at all. mysql is excluded on purpose — no
    # disconnect-type coverage, so we don't advertise resilience we can't give.
    assert not is_pooled_sql_url("mysql+aiomysql://u:p@host/db")
    assert not is_pooled_sql_url("sqlite:///tmp/x.db")
    assert not is_pooled_sql_url("sqlite+aiosqlite:///tmp/x.db")
    assert not is_pooled_sql_url(
        "agentengine://projects/p/locations/l/reasoningEngines/1"
    )
    assert not is_pooled_sql_url(None)
    assert not is_pooled_sql_url("")


def test_db_resilience_has_no_module_level_asyncpg_import():
    # asyncpg is a `postgres`-extra; a core-only install must still import
    # db_resilience / fast_api_app and call build_runner().
    from pathlib import Path

    from app.infrastructure import db_resilience

    src = Path(db_resilience.__file__).read_text()
    assert "\nimport asyncpg" not in src


def test_transient_disconnect_types_degrade_without_asyncpg(monkeypatch):
    import sys

    from app.infrastructure import db_resilience as dbr

    monkeypatch.setitem(sys.modules, "asyncpg", None)  # forces ImportError
    dbr._transient_disconnect_types.cache_clear()
    try:
        assert dbr._transient_disconnect_types() == (
            ConnectionRefusedError,
            ConnectionResetError,
        )
    finally:
        dbr._transient_disconnect_types.cache_clear()


def test_task_store_engine_has_pre_ping(monkeypatch):
    from app.fast_api_app import _build_task_store

    monkeypatch.setenv("TASK_DB_URL", "sqlite+aiosqlite://")
    monkeypatch.delenv("USE_IN_MEMORY_TASK_STORE", raising=False)
    store = _build_task_store()
    pool = store.engine.sync_engine.pool
    assert pool._pre_ping is True
    assert pool._recycle == resilient_engine_kwargs()["pool_recycle"]


def test_task_store_sqlite_builds_without_queuepool_sizing(monkeypatch):
    # The pooled=False path must not pass QueuePool-only args (pool_size/…) to
    # sqlite's StaticPool — create_async_engine would raise if it did.
    from sqlalchemy.pool import QueuePool

    from app.fast_api_app import _build_task_store

    monkeypatch.setenv("TASK_DB_URL", "sqlite+aiosqlite://")
    monkeypatch.delenv("USE_IN_MEMORY_TASK_STORE", raising=False)
    store = (
        _build_task_store()
    )  # raises here if pool_size reached the StaticPool
    assert not isinstance(store.engine.sync_engine.pool, QueuePool)


def test_default_task_db_lives_under_data_dir(monkeypatch):
    from app.fast_api_app import _build_task_store

    # No explicit TASK_DB_URL / in-memory flag => dev default sqlite is
    # relocated under .data/ to keep the repo root clean.
    monkeypatch.delenv("TASK_DB_URL", raising=False)
    monkeypatch.delenv("USE_IN_MEMORY_TASK_STORE", raising=False)
    store = _build_task_store()
    assert "/.data/lha_tasks.db" in str(store.engine.url)


def test_resilient_engine_kwargs_set_connect_timeout():
    kwargs = resilient_engine_kwargs()
    # connect_args carries the per-driver connect timeout (seconds)
    assert kwargs["connect_args"]["timeout"] == 10


def test_is_transient_disconnect_matches_known_drops():
    import asyncpg
    from sqlalchemy.exc import DBAPIError

    from app.infrastructure.db_resilience import is_transient_disconnect

    assert is_transient_disconnect(
        asyncpg.exceptions.ConnectionDoesNotExistError()
    )
    assert is_transient_disconnect(
        ConnectionRefusedError(111, "Connection refused")
    )
    # SQLAlchemy wraps driver errors; a disconnect-flagged DBAPIError counts
    wrapped = DBAPIError.instance(
        "SELECT 1",
        {},
        ConnectionRefusedError(111, "x"),
        Exception,
        connection_invalidated=True,
    )
    assert is_transient_disconnect(wrapped)


def test_is_transient_disconnect_rejects_non_transient():
    from app.infrastructure.db_resilience import is_transient_disconnect

    assert not is_transient_disconnect(ValueError("bad sql"))
    assert not is_transient_disconnect(KeyError("missing"))


@pytest.mark.asyncio
async def test_retry_on_disconnect_retries_then_succeeds():
    from app.infrastructure.db_resilience import retry_on_disconnect

    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError()
        return "ok"

    result = await retry_on_disconnect(op, attempts=3, base_delay=0)
    assert result == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_on_disconnect_gives_up_after_attempts():
    from app.infrastructure.db_resilience import retry_on_disconnect

    async def op():
        raise ConnectionRefusedError(111, "Connection refused")

    with pytest.raises(ConnectionRefusedError):
        await retry_on_disconnect(op, attempts=3, base_delay=0)


@pytest.mark.asyncio
async def test_retry_on_disconnect_does_not_retry_application_errors():
    from app.infrastructure.db_resilience import retry_on_disconnect

    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise ValueError("bad sql")

    with pytest.raises(ValueError):
        await retry_on_disconnect(op, attempts=3, base_delay=0)
    assert calls["n"] == 1  # not retried


def test_build_app_passes_session_db_kwargs_to_adk_web_path(monkeypatch):
    import app.fast_api_app as faa

    captured = {}

    def fake_get_fast_api_app(**kwargs):
        captured.update(kwargs)
        from fastapi import FastAPI

        return FastAPI()

    # Patch the symbols where _build_app resolves them (_resolve_service_uris is
    # module-level in fast_api_app; get_fast_api_app comes from the ADK module).
    import google.adk.cli.fast_api as adk_fast_api

    monkeypatch.setattr(
        faa,
        "_resolve_service_uris",
        lambda: ("postgresql+asyncpg://u:p@/db?host=/cloudsql/x", None, None),
    )
    monkeypatch.setattr(adk_fast_api, "get_fast_api_app", fake_get_fast_api_app)

    faa._build_app()
    assert captured["session_db_kwargs"] == resilient_engine_kwargs()


def test_lifespan_session_service_is_retry_wrapped_for_pooled_url(monkeypatch):
    import app.fast_api_app as faa
    from app.infrastructure.resilient_session_service import (
        ResilientSessionService,
    )

    wrapped = faa._maybe_resilient_session_service(
        object(), "postgresql+asyncpg://u:p@/db?host=/cloudsql/x"
    )
    assert isinstance(wrapped, ResilientSessionService)

    plain = faa._maybe_resilient_session_service(object(), "sqlite:///x.db")
    assert not isinstance(plain, ResilientSessionService)


@pytest.mark.asyncio
async def test_task_store_save_retries_on_disconnect():
    from app.fast_api_app import _ResilientTaskStore

    class _Inner:
        def __init__(self):
            self.calls = 0

        async def save(self, task):
            self.calls += 1
            if self.calls == 1:
                raise asyncpg.exceptions.ConnectionDoesNotExistError()

    inner = _Inner()
    store = _ResilientTaskStore.__new__(_ResilientTaskStore)
    store._delegate = inner
    store._retry_base_delay = 0
    await store.save(object())
    assert inner.calls == 2


def test_task_store_unwrapped_attr_passes_through_to_delegate():
    from app.fast_api_app import _ResilientTaskStore

    class _Inner:
        marker = "from-inner"

        def some_future_method(self):
            return "delegated"

    inner = _Inner()
    store = _ResilientTaskStore(inner)
    assert store.marker == "from-inner"
    assert store.some_future_method() == "delegated"


def test_task_store_getattr_raises_for_genuinely_missing_attr():
    from app.fast_api_app import _ResilientTaskStore

    class _Inner:
        pass

    store = _ResilientTaskStore(_Inner())
    with pytest.raises(AttributeError):
        _ = store.does_not_exist_anywhere
