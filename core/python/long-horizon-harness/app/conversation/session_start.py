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

"""``on_session_start`` parity hook for ADK's ``before_agent_callback``.

A fresh session must give plugins / callbacks a chance to initialize state
before any model call. ADK has no built-in equivalent; we encode "first
invocation" by setting a state flag the first time the callback fires and
skipping subsequent re-stamps.

Beyond the first-turn marker, the callback installs a per-session
``Environment`` (the storage abstraction used by skills and tools) via
``set_active_environment``. This module owns the process-wide orchestration —
the per-``user_id`` env cache, provisioning locks, ContextVar binding,
per-turn auth refresh, and atexit teardown — but delegates all
backend-specific provisioning/reattach/snapshot/upgrade/auth work to the active
``SandboxProvider`` (``app/sandbox/provider.py``), selected by
``LHA_ENVIRONMENT_BACKEND`` or overridden via ``set_sandbox_provider``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.adk.environment import BaseEnvironment

from app.context.compaction_context import (
    CompactionContext,
    bind_compaction_context,
)
from app.environment_context import (
    environment_provider,
    sandbox_provider,
    set_active_environment,
)
from app.guardrails import clear_halt_state
from app.infrastructure.constants import TITLE_KEY, TITLE_MAX_LEN
from app.infrastructure.env import env_str
from app.memory.user_profile import load_user_profile
from app.routines.run_context import RoutineRun, active_routine_run
from app.sandbox.provider import (
    LocalProvider,
    PendingMigration,
    SandboxConfigurationError,
    SandboxProvider,
    VertexSandboxProvider,
    snapshots_enabled,
)

logger = logging.getLogger(__name__)

SESSION_STARTED_STATE_KEY = "_session_started"
SESSION_STARTED_AT_STATE_KEY = "_session_started_at"

# Backward-compat alias: some tests/callers reference the old private name.
_PendingMigration = PendingMigration

_env_cache: dict[tuple[str, str], BaseEnvironment] = {}
# Per-user asyncio lock around _ensure_environment so warm-POST and the
# on_session_start callback can't both miss the cache and double-provision
# within a single backend instance. Cross-instance races are bounded by the
# provider's sandboxes.list discovery + the lex-smallest dedup in
# find_user_sandbox.
_provisioning_locks: dict[str, asyncio.Lock] = {}
_close_lock = threading.Lock()
_closed_already = False


def _close_envs_at_exit() -> None:
    # Sandboxes are long-lived now — close just shuts the local HTTP
    # client so we don't leak sockets. The platform-side sandbox stays
    # alive for the next process to reattach.
    global _closed_already
    with _close_lock:
        if _closed_already:
            return
        _closed_already = True
        envs = list(_env_cache.values())
        _env_cache.clear()

    for env in envs:
        close_sync = getattr(env, "close_sync", None)
        if close_sync is None:
            continue
        try:
            close_sync()
        except Exception as exc:
            logger.warning("env close failed at exit: %s", exc)


atexit.register(_close_envs_at_exit)


def _resolve_backend() -> str:
    """Normalized ``LHA_ENVIRONMENT_BACKEND`` value (``sandbox`` default)."""
    return env_str("LHA_ENVIRONMENT_BACKEND", "sandbox").strip().lower()


def _resolve_provider() -> SandboxProvider:
    """The active sandbox provider: an explicit ``set_sandbox_provider`` override
    wins, else the built-in selected by ``LHA_ENVIRONMENT_BACKEND``."""
    override = sandbox_provider()
    if override is not None:
        return override
    if _resolve_backend() == "sandbox":
        return VertexSandboxProvider()
    return LocalProvider()


def _build_environment(
    user_id: str,
) -> tuple[BaseEnvironment, PendingMigration | None]:
    # The per-user env-factory override (bring-your-own env, skip the provider's
    # provisioning) short-circuits, mirroring the pre-provider behavior.
    override = environment_provider()
    if override is not None:
        return override(user_id), None
    return _resolve_provider().build_environment(user_id)


def _build_routine_environment(
    run: RoutineRun,
) -> tuple[BaseEnvironment, PendingMigration | None]:
    """Build a FRESH, isolated environment for a routine run — never the user's.
    Delegates to the active provider's routine path (a routine-scoped local dir,
    or the routine's own ``lhart-<id>`` sandbox). No migration/upgrade/snapshot."""
    return _resolve_provider().build_routine_environment(run)


@dataclass(frozen=True)
class MigrationOutcome:
    """Result of a ``/workspace`` migration, surfaced so a toggle/upgrade can tell
    the user whether their files came across. ``attempted=False`` = fresh provision
    with nothing to migrate."""

    attempted: bool
    ok: bool
    num_bytes: int = 0
    source_deleted: bool = False
    error: str | None = None


async def _migrate_workspace(
    new_env: Any, pending: PendingMigration
) -> MigrationOutcome:
    """Best-effort copy of ``/workspace`` from a prior sandbox into the freshly
    provisioned one, then retire the prior. A copy failure leaves the prior intact
    (ages out via TTL) so a bug can never lose files — reported via the returned
    outcome so the caller can warn the user their files stayed behind."""
    from app.sandbox.lifecycle import delete_sandbox

    source = pending.source_env
    workspace = new_env.working_dir
    logger.info(
        "workspace migration: copying /workspace %s -> %s",
        pending.source_name,
        new_env.sandbox_name,
    )
    try:
        data = await source.download_zip(workspace)
        await new_env.upload_zip(workspace, data)
    except Exception as exc:
        logger.warning(
            "workspace migration: copy from prior sandbox %s failed (%s); "
            "leaving it intact for TTL cleanup",
            pending.source_name,
            exc,
        )
        await _close_quietly(source)
        return MigrationOutcome(attempted=True, ok=False, error=str(exc))

    num_bytes = len(data) if isinstance(data, bytes | bytearray) else 0
    logger.info(
        "workspace migration: copied %d bytes from %s into %s",
        num_bytes,
        pending.source_name,
        new_env.sandbox_name,
    )
    deleted = False
    try:
        delete_sandbox(pending.client, sandbox_name=pending.source_name)
        deleted = True
    except Exception as exc:
        logger.warning(
            "workspace migration: copied but failed to delete prior %s (%s)",
            pending.source_name,
            exc,
        )
    await _close_quietly(source)
    return MigrationOutcome(
        attempted=True, ok=True, num_bytes=num_bytes, source_deleted=deleted
    )


async def _close_quietly(env: Any) -> None:
    try:
        await env.close()
    except Exception as exc:
        logger.warning("migration: closing source env raised: %s", exc)


def _will_attempt_sandbox() -> bool:
    """Whether the active provider will actually try to provision — drives
    whether we surface a provisioning banner to the UI."""
    return _resolve_provider().will_provision()


def get_provisioning_status(user_id: str) -> dict[str, Any] | None:
    """Snapshot of sandbox provisioning state for ``user_id`` for the UI banner.
    Derived from the provider (Vertex's authoritative list for the sandbox
    backend), so every Cloud Run instance returns the same answer. ``None`` for
    the local backend / when no sandbox exists yet. Read by ``/lha/state``."""
    return _resolve_provider().provisioning_status(user_id)


async def peek_environment(user_id: str) -> BaseEnvironment | None:
    """The user's environment if one already exists and reattaches, else None.
    NEVER provisions — passive reads (e.g. /lha/processes) must not spin up a
    sandbox. Local backend is instant, so it always resolves."""
    if _resolve_backend() == "local":
        return await _ensure_environment(user_id)
    status = get_provisioning_status(user_id)
    if not status or status.get("status") != "ready":
        return None
    return await _ensure_environment(user_id)


async def _finalize_env(
    env: BaseEnvironment, pending: PendingMigration | None
) -> MigrationOutcome:
    """Bring a freshly-built env online: initialize, then run the pending workspace
    migration and return its outcome. Raises if ``initialize`` fails, after closing
    the env + migration source."""
    try:
        await env.initialize()
    except BaseException:
        try:
            await env.close()
        except Exception as exc:
            logger.warning(
                "env.close() after failed initialize raised: %s", exc
            )
        if pending is not None:
            await _close_quietly(pending.source_env)
        raise
    if pending is not None:
        return await _migrate_workspace(env, pending)
    return MigrationOutcome(attempted=False, ok=True)


async def _ensure_environment(user_id: str) -> BaseEnvironment:
    run = active_routine_run()
    if run is not None:
        cache_key = ("routine", run.routine_id)
        lock_key = f"routine:{run.routine_id}"

        def _build() -> tuple[BaseEnvironment, PendingMigration | None]:
            return _build_routine_environment(run)
    else:
        cache_key = (_resolve_backend(), user_id)
        lock_key = user_id

        def _build() -> tuple[BaseEnvironment, PendingMigration | None]:
            return _build_environment(user_id)

    env = _env_cache.get(cache_key)
    if env is not None:
        # JWT TTL (~1h) is shorter than sandbox lifetime, so every cache hit
        # re-mints before returning. UI workspace endpoints rely on this —
        # they don't go through on_session_start_callback.
        await _refresh_sandbox_auth(env)
        # _refresh_sandbox_auth evicts the entry if the sandbox 404s (deleted by
        # a cross-instance upgrade); fall through to rebuild if so.
        if _env_cache.get(cache_key) is env:
            return env

    lock = _provisioning_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        env = _env_cache.get(cache_key)
        if env is not None:
            await _refresh_sandbox_auth(env)
            if _env_cache.get(cache_key) is env:
                return env

        env, pending = _build()
        await _finalize_env(env, pending)
        _env_cache[cache_key] = env
        return env


async def upgrade_user_sandbox(user_id: str) -> dict[str, Any]:
    """Explicitly upgrade ``user_id``'s sandbox to the current
    ``LHA_RUNTIME_IMAGE``: the provider provisions a fresh current-image sandbox
    (migrating ``/workspace`` from the prior); this orchestrator finalizes it and
    hot-swaps the cached + active environment so the running session continues.

    Status values: ``upgraded``, ``already_current``, ``no_sandbox``,
    ``unavailable``."""
    prov = _resolve_provider().provision_upgrade(user_id)
    if prov.status != "provisioned":
        result: dict[str, Any] = {"status": prov.status}
        if prov.version:
            result["version"] = prov.version
        if prov.reason:
            result["reason"] = prov.reason
        return result

    new_env, migration = await _hotswap_env(user_id, prov)
    return {
        "status": "upgraded",
        "version": prov.version,
        "sandbox_name": new_env.sandbox_name,
        "migration": _migration_dict(migration),
    }


def _migration_dict(m: MigrationOutcome) -> dict[str, Any]:
    d: dict[str, Any] = {
        "attempted": m.attempted,
        "ok": m.ok,
        "num_bytes": m.num_bytes,
        "source_deleted": m.source_deleted,
    }
    if m.error:
        d["error"] = m.error
    return d


async def _hotswap_env(
    user_id: str, prov: Any
) -> tuple[BaseEnvironment, MigrationOutcome]:
    """Finalize a provider-provisioned env and hot-swap it for the user's cached +
    active env under the per-user lock, retiring the old one. Returns the new env
    and the workspace-migration outcome."""
    cache_key = (_resolve_backend(), user_id)
    lock = _provisioning_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        new_env = prov.new_env
        migration = await _finalize_env(new_env, prov.pending)
        old_env = _env_cache.get(cache_key)
        _env_cache[cache_key] = new_env
        set_active_environment(new_env)
        if old_env is not None and old_env is not new_env:
            await _close_quietly(old_env)
    return new_env, migration


def _safe_user_id(callback_context: Any) -> str:
    user_id = getattr(callback_context, "user_id", None)
    return str(user_id) if user_id else "anonymous"


async def _refresh_sandbox_auth(env: BaseEnvironment) -> None:
    """Re-mint per-turn auth on the active env (env-owned; the light
    ``set_environment_provider`` hook gets refresh too). On a gone backend
    (``refresh_auth`` returns ``False``) evict the cached entry so the next
    ``_ensure_environment`` rebuilds instead of serving a 404-ing cache hit."""
    alive = await env.refresh_auth()
    if alive:
        return
    owner = getattr(env, "owner", None)
    if owner:
        _env_cache.pop((_resolve_backend(), owner), None)
    logger.info(
        "sandbox %s gone; evicted cached env for %s",
        getattr(env, "sandbox_name", "?"),
        owner,
    )


def _bind_compaction_ctx_from(callback_context: Any, user_id: str) -> None:
    invocation_context = getattr(callback_context, "_invocation_context", None)
    memory_service = getattr(invocation_context, "memory_service", None)
    app_name = getattr(invocation_context, "app_name", "") or ""
    if memory_service is None or not app_name or not user_id:
        return
    # Local import — app.agent imports from this module, so a top-level
    # import would cycle.
    from app.agent import SIBLING_AGENT_PLUGIN

    bind_compaction_context(
        CompactionContext(
            memory_service=memory_service,
            app_name=app_name,
            user_id=user_id,
            sibling_agent_plugin=SIBLING_AGENT_PLUGIN,
        )
    )


def _stamp_chat_title(state: Any, user_content: Any) -> None:
    if not user_content or state.get(TITLE_KEY):
        return
    parts = getattr(user_content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            state[TITLE_KEY] = text.strip().splitlines()[0][:TITLE_MAX_LEN]
            return


async def on_session_start_callback(callback_context: Any) -> Any | None:
    user_id = _safe_user_id(callback_context)
    env = await _ensure_environment(user_id)
    # Re-bind on every turn — ContextVar values don't survive across
    # asyncio.Task boundaries, so each invocation needs to set it again.
    set_active_environment(env)
    _bind_compaction_ctx_from(callback_context, user_id)

    state = getattr(callback_context, "state", None)
    if state is None:
        return None
    # Each user message is a fresh invocation — clear any halt latched on
    # a prior turn so the agent gets one chance per message. The
    # short-circuit response from the previous turn already informed the
    # user; continuing implies acknowledgement.
    clear_halt_state(state)
    if state.get(SESSION_STARTED_STATE_KEY):
        return None
    state[SESSION_STARTED_STATE_KEY] = True
    state[SESSION_STARTED_AT_STATE_KEY] = datetime.now(UTC).isoformat()

    # Stamp the sidebar title from the first user message on the first turn, so
    # the /lha/sessions list reads it from state instead of scanning events
    # (where a recent-events window would otherwise pick a recent message).
    _stamp_chat_title(state, getattr(callback_context, "user_content", None))

    # Load the user's structured profile once per session so the system-prompt
    # ``## User Profile`` block (render_user_profile) has something to render.
    # Set even when empty so we don't re-fetch on every turn.
    invocation_context = getattr(callback_context, "_invocation_context", None)
    state["user_profile"] = await load_user_profile(
        memory_service=getattr(invocation_context, "memory_service", None),
        app_name=getattr(invocation_context, "app_name", "") or "",
        user_id=getattr(invocation_context, "user_id", "") or user_id,
    )
    return None


__all__ = [
    "SESSION_STARTED_AT_STATE_KEY",
    "SESSION_STARTED_STATE_KEY",
    "SandboxConfigurationError",
    "get_provisioning_status",
    "on_session_start_callback",
    "snapshots_enabled",
    "upgrade_user_sandbox",
]
