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

"""Connection-resilience defaults for the SQL-backed session/task stores.

A Cloud SQL maintenance event or HA failover briefly drops connections. Without
these, the pooled connections go stale and every request 500s until the Cloud
Run instances are cycled by hand. `pool_pre_ping` discards a dead connection on
checkout and opens a fresh one; `pool_recycle` caps connection lifetime so they
rotate before the server times them out; `connect_args.timeout` bounds a refused
connect (asyncpg) so it fails fast and is retried. `pool_pre_ping` only validates
at checkout — `is_transient_disconnect` + `retry_on_disconnect` cover the
mid-flight and connect-time drops it cannot.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_ATTEMPTS = 3
_DEFAULT_BASE_DELAY = 0.25

# Recycle pooled connections well under Cloud SQL's idle timeout so one that
# went stale across a blip is replaced rather than handed out dead.
_POOL_RECYCLE_SECONDS = 1800

# Fail a connect attempt fast so a refused Cloud SQL proxy socket (instance
# restart / failover) is retried instead of hanging on the default no-timeout.
_CONNECT_TIMEOUT_SECONDS = 10

# Bound the per-engine connection footprint. Each Cloud Run instance holds 3
# SQLAlchemy engines (lifespan session service, ADK web-path session service,
# A2A task store) + 2 asyncpg scheduler pools; unbounded pools (SQLAlchemy's
# 5+10 default) blow past Cloud SQL's max_connections once a few instances run.
# Defaults sized for concurrency=8 so a hot engine doesn't queue; tune per deploy
# via env if the instance/connection math changes. See docs/security-model.md.
# pool_timeout is BACKPRESSURE, not failure: when the pool is saturated a checkout
# waits this long then raises QueuePool TimeoutError — deliberately NOT in
# TRANSIENT_DISCONNECT_TYPES, so it surfaces rather than retrying a dead pool.
_DEFAULT_POOL_SIZE = 4
_DEFAULT_MAX_OVERFLOW = 4
_DEFAULT_POOL_TIMEOUT_SECONDS = 30

# Postgres only: MySQL has no disconnect-type coverage below, so advertising it
# here would grant checkout pinging but zero mid-flight retry. Add aiomysql/
# pymysql disconnect types to _transient_disconnect_types() before re-adding it.
_POOLED_SQL_SCHEME_PREFIXES = ("postgresql", "postgres")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# Errors that mean "the connection died, a retry on a fresh one will work" —
# not application/SQL errors. pool_pre_ping only covers checkout; these cover
# the mid-flight and connect-time drops it can't. asyncpg is a `postgres`-extra
# import, so resolve it lazily: a core-only install (no asyncpg) still imports
# db_resilience / fast_api_app and keeps the stdlib connect-error coverage.
@lru_cache(maxsize=1)
def _transient_disconnect_types() -> tuple[type[BaseException], ...]:
    types: tuple[type[BaseException], ...] = (
        ConnectionRefusedError,
        ConnectionResetError,
    )
    try:
        import asyncpg
    except ImportError:
        return types
    return (
        asyncpg.exceptions.ConnectionDoesNotExistError,
        asyncpg.exceptions.InterfaceError,
        asyncpg.exceptions.ConnectionFailureError,
        *types,
    )


def resilient_engine_kwargs(pooled: bool = True) -> dict[str, Any]:
    """Engine kwargs for a SQL-backed store.

    ``pooled`` gates the QueuePool-only sizing (``pool_size``/``max_overflow``/
    ``pool_timeout``): pass ``False`` for sqlite, whose StaticPool/NullPool reject
    those args. The always-safe resilience knobs are returned either way.
    """
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": _POOL_RECYCLE_SECONDS,
        "connect_args": {"timeout": _CONNECT_TIMEOUT_SECONDS},
    }
    if pooled:
        kwargs["pool_size"] = _env_int("LHA_DB_POOL_SIZE", _DEFAULT_POOL_SIZE)
        kwargs["max_overflow"] = _env_int(
            "LHA_DB_MAX_OVERFLOW", _DEFAULT_MAX_OVERFLOW
        )
        kwargs["pool_timeout"] = _env_int(
            "LHA_DB_POOL_TIMEOUT", _DEFAULT_POOL_TIMEOUT_SECONDS
        )
    return kwargs


def is_transient_disconnect(exc: BaseException) -> bool:
    # SQLAlchemy flags wrapped driver disconnects with connection_invalidated.
    if isinstance(exc, (DBAPIError, OperationalError, InterfaceError)):
        if getattr(exc, "connection_invalidated", False):
            return True
        orig = getattr(exc, "orig", None)
        if orig is not None and isinstance(orig, _transient_disconnect_types()):
            return True
    return isinstance(exc, _transient_disconnect_types())


def is_pooled_sql_url(uri: str | None) -> bool:
    """True only for server-based SQL URLs (postgres).

    These route to ADK's `DatabaseSessionService`, which forwards engine kwargs.
    Excludes sqlite (a local file → `SqliteSessionService`, which ignores the
    kwargs and warns), `agentengine://`, in-memory, and None — pooling
    resilience is meaningless for those, and the session factory drops the
    kwargs rather than applying them.
    """
    if not uri or "://" not in uri:
        return False
    scheme = uri.split("://", 1)[0].lower().split("+", 1)[0]
    return scheme in _POOLED_SQL_SCHEME_PREFIXES


async def retry_on_disconnect(
    op: Callable[[], Awaitable[_T]],
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    base_delay: float = _DEFAULT_BASE_DELAY,
) -> _T:
    """Run ``op`` again on a transient DB disconnect; re-raise everything else.

    Closes the gap pool_pre_ping can't: a connection severed mid-query (Cloud
    SQL restart/failover) or a refused connect against a proxy that's mid-bounce.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except BaseException as exc:
            if not is_transient_disconnect(exc):
                raise
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "db: transient disconnect (attempt %d/%d), retrying in %.2fs: %r",
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None  # noqa: S101 -- narrow type after retry loop
    raise last_exc
