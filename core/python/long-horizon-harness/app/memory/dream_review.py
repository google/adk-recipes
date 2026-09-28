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

"""Cross-session memory review — the nightly "dream" pass.

The dream review looks at a user's most recent sessions and asks Memory
Bank to consolidate them into a native Structured Profile (the schema is
declared in ``app/infrastructure/memory_config.py``). Generation happens
server-side via ``memories.generate``; the live agent reads the result
back with ``retrieve_profiles`` (see ``app/memory/user_profile.py``).

The same ``generate`` call also consolidates the user's general memories
(dedupe + contradiction reconciliation) — on by default, gated by
``LHA_MEMORY_CONSOLIDATION`` — and the success envelope carries the
per-pass ``consolidation`` action counts (``created``/``updated``/``deleted``).

Recent sessions are read from the ``session_service`` (Database/InMemory).
The pass is callable on demand (``request_dream_review`` / ``/dream-review``)
and from the nightly ``/scheduler/dream-review`` cron, which enumerates
active users via :func:`list_active_users`. When prerequisites are missing
(no session service, no recent sessions, non-Vertex memory) the pipeline
returns a structured ``{success: false, reason: ...}`` envelope rather than
raising.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.infrastructure.constants import (
    SCHEDULER_SOURCE,
    SESSION_SOURCE_KEY,
)
from app.memory.adapter import memory_adapter

logger = logging.getLogger(__name__)

DREAM_REVIEW_ENABLED_ENV = "LHA_DREAM_REVIEW"
DREAM_SESSION_LIMIT_ENV = "LHA_DREAM_SESSION_LIMIT"
DREAM_LOOKBACK_HOURS_ENV = "LHA_DREAM_LOOKBACK_HOURS"
MEMORY_CONSOLIDATION_ENABLED_ENV = "LHA_MEMORY_CONSOLIDATION"

_DEFAULT_SESSION_LIMIT = 50
_DEFAULT_LOOKBACK_HOURS = 24.0


def _session_limit() -> int:
    try:
        value = int(os.environ.get(DREAM_SESSION_LIMIT_ENV, ""))
    except (TypeError, ValueError):
        return _DEFAULT_SESSION_LIMIT
    return value if value > 0 else _DEFAULT_SESSION_LIMIT


def lookback_seconds() -> float:
    try:
        hours = float(os.environ.get(DREAM_LOOKBACK_HOURS_ENV, ""))
    except (TypeError, ValueError):
        hours = _DEFAULT_LOOKBACK_HOURS
    if hours <= 0:
        hours = _DEFAULT_LOOKBACK_HOURS
    return hours * 3600.0


def _has_text(event: Any) -> bool:
    """True if the event carries conversational text (skip pure tool-call noise)."""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or [] if content else []
    return any(getattr(part, "text", None) for part in parts)


def _consolidation_enabled() -> bool:
    return os.environ.get(MEMORY_CONSOLIDATION_ENABLED_ENV, "1") != "0"


def _is_scheduler_session(session: Any) -> bool:
    """True for sessions a cron job created itself.

    Scheduler-created sessions live in the same store dream-review reads
    from; skipping them keeps a user from looking "active" forever and
    keeps cron output out of the generation input.
    """
    state = getattr(session, "state", None) or {}
    return state.get(SESSION_SOURCE_KEY) == SCHEDULER_SOURCE


async def list_active_users(
    session_service: Any, *, app_name: str, since_ts: float
) -> list[str]:
    """User ids with a real session updated at/after ``since_ts``.

    The nightly cron sends an empty ``user_ids`` list; the endpoint calls
    this to discover who to dream about. ``user_id=None`` lists every
    user's sessions (supported by Database/InMemory session services).
    Scheduler-created sessions are ignored — see :func:`_is_scheduler_session`.
    """
    fn = getattr(session_service, "list_sessions", None)
    if not callable(fn):
        return []
    try:
        response = await fn(app_name=app_name, user_id=None)
    except Exception:
        logger.exception("dream_review: list_sessions(all users) failed")
        return []
    users: set[str] = set()
    for session in getattr(response, "sessions", None) or []:
        user_id = getattr(session, "user_id", None)
        last = getattr(session, "last_update_time", 0) or 0
        if user_id and last >= since_ts and not _is_scheduler_session(session):
            users.add(user_id)
    return sorted(users)


async def _collect_user_sessions(
    session_service: Any, *, app_name: str, user_id: str, limit: int
) -> list[dict[str, Any]] | None:
    """Load a user's ``limit`` most recent sessions, newest first.

    ``list_sessions`` returns sessions without their events, so each one is
    re-fetched via ``get_session`` (one round-trip per session, bounded by
    ``limit``) to populate the snapshot fed to Memory Bank. Scheduler-created
    sessions are skipped so the pass never dreams over its own output.
    Returns ``None`` if the service can't list/get sessions.
    """
    list_fn = getattr(session_service, "list_sessions", None)
    get_fn = getattr(session_service, "get_session", None)
    if not callable(list_fn) or not callable(get_fn):
        return None
    try:
        response = await list_fn(app_name=app_name, user_id=user_id)
    except Exception:
        logger.exception("dream_review: list_sessions(%s) failed", user_id)
        return None
    sessions = [
        s
        for s in (getattr(response, "sessions", None) or [])
        if not _is_scheduler_session(s)
    ]
    sessions.sort(
        key=lambda s: getattr(s, "last_update_time", 0) or 0, reverse=True
    )
    out: list[dict[str, Any]] = []
    for session in sessions[:limit]:
        session_id = getattr(session, "id", None)
        if not session_id:
            continue
        try:
            full = await get_fn(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
        except Exception:
            logger.exception("dream_review: get_session(%s) failed", session_id)
            continue
        events = (getattr(full, "events", None) or []) if full else []
        out.append({"session_id": session_id, "events": events})
    return out


async def _run_dream_review_for_user(
    *,
    memory_service: Any,
    app_name: str,
    user_id: str,
    session_service: Any | None = None,
) -> dict[str, Any]:
    """Run a dream-review pass for a single user.

    Collects the user's recent session events from ``session_service`` and
    hands them to Memory Bank's ``generate`` so it consolidates the user's
    Structured Profile and general memories server-side. Returns a success
    envelope carrying ``sessions_reviewed`` and a ``consolidation`` block
    (``{ran, created, updated, deleted}``, or ``{ran: False, reason}`` when
    the kill switch is off). Callable from the model tool
    (``request_dream_review``) or the scheduler HTTP endpoint.
    """
    if os.environ.get(DREAM_REVIEW_ENABLED_ENV, "1") == "0":
        return {
            "success": False,
            "reason": "dream review disabled by LHA_DREAM_REVIEW env",
        }

    if not app_name or not user_id:
        return {"success": False, "reason": "no app_name / user_id"}

    if session_service is None:
        return {
            "success": False,
            "reason": "no session service for session lookup",
        }

    adapter = memory_adapter(memory_service)
    if not adapter.supports_profiles():
        return {
            "success": False,
            "reason": "structured profiles require Vertex Memory Bank",
        }

    sessions = await _collect_user_sessions(
        session_service,
        app_name=app_name,
        user_id=user_id,
        limit=_session_limit(),
    )
    if sessions is None:
        return {
            "success": False,
            "reason": (
                "session service does not support session lookup "
                "(needs list_sessions/get_session)"
            ),
        }

    events = [
        event
        for session in sessions
        for event in session["events"]
        if _has_text(event)
    ]
    if not events:
        return {
            "success": False,
            "reason": "no recent session content to review",
        }

    consolidate = _consolidation_enabled()
    try:
        counts = await adapter.generate_profile(
            app_name=app_name,
            user_id=user_id,
            events=events,
            consolidate=consolidate,
        )
    except Exception:
        logger.exception("dream_review: generate failed for %s", user_id)
        return {"success": False, "reason": "profile generation failed"}

    if consolidate:
        consolidation = {"ran": True, **counts}
        logger.warning(
            "dream_review: consolidated %s created=%d updated=%d deleted=%d",
            user_id,
            counts["created"],
            counts["updated"],
            counts["deleted"],
        )
    else:
        consolidation = {"ran": False, "reason": "disabled"}

    return {
        "success": True,
        "sessions_reviewed": len(sessions),
        "consolidation": consolidation,
    }


async def request_dream_review(*, tool_context: Any) -> dict[str, Any]:
    """Consolidate the user's profile from their recent cross-session history."""
    invocation_context = getattr(tool_context, "_invocation_context", None)
    if invocation_context is None:
        return {
            "success": False,
            "reason": "no active invocation context",
        }
    return await _run_dream_review_for_user(
        memory_service=getattr(invocation_context, "memory_service", None),
        app_name=getattr(invocation_context, "app_name", "") or "",
        user_id=getattr(invocation_context, "user_id", "") or "",
        session_service=getattr(invocation_context, "session_service", None),
    )


__all__ = [
    "DREAM_REVIEW_ENABLED_ENV",
    "MEMORY_CONSOLIDATION_ENABLED_ENV",
    "_run_dream_review_for_user",
    "list_active_users",
    "lookback_seconds",
    "request_dream_review",
]
