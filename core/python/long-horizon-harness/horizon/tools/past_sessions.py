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

"""recall_past_sessions_entries: surface the current user's other chat
sessions. The model-facing surface is memory(action='search',...) in the
memory tool module — this module keeps only the pure, Runner-independent
helper it calls.

Two modes on one call:
- session_id=None  -> index: recent sessions with derived titles.
- session_id=<id>  -> drill: events (user + assistant text) for that session.

Bounded by user_id from the invocation context; the current session is never
returned.
"""

from __future__ import annotations

from typing import Any

from google.adk.sessions import BaseSessionService

DEFAULT_INDEX_LIMIT = 20
MAX_INDEX_LIMIT = 50
DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 200
TITLE_MAX_LEN = 80
EVENT_TEXT_MAX_LEN = 800


def _derive_title(session: Any) -> str:
    explicit = session.state.get("lha_chat_title") if session.state else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:TITLE_MAX_LEN]
    for event in session.events:
        if getattr(event, "author", None) != "user":
            continue
        content = getattr(event, "content", None)
        if not content or not content.parts:
            continue
        for part in content.parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip().splitlines()[0][:TITLE_MAX_LEN]
    return "Untitled chat"


def _started_at(session: Any) -> float:
    for event in session.events:
        ts = getattr(event, "timestamp", None)
        if isinstance(ts, (int, float)):
            return float(ts)
    return float(session.last_update_time)


def _event_text(event: Any) -> str | None:
    content = getattr(event, "content", None)
    if not content or not content.parts:
        return None
    chunks: list[str] = []
    for part in content.parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    if not chunks:
        return None
    joined = "\n".join(chunks)
    if len(joined) > EVENT_TEXT_MAX_LEN:
        return joined[:EVENT_TEXT_MAX_LEN] + "…"
    return joined


def _normalize_author(author: str | None) -> str:
    if author == "user":
        return "user"
    return "assistant"


async def recall_past_sessions_entries(
    *,
    session_service: BaseSessionService,
    app_name: str,
    user_id: str,
    current_session_id: str | None,
    session_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Pure helper: same surface as the LLM-callable tool, no ToolContext."""
    if session_id is None:
        cap = min(max(limit or DEFAULT_INDEX_LIMIT, 1), MAX_INDEX_LIMIT)
        try:
            resp = await session_service.list_sessions(
                app_name=app_name, user_id=user_id
            )
        except Exception as err:
            return {"success": False, "error": f"list_sessions failed: {err}"}

        stubs = [s for s in resp.sessions if s.id != current_session_id]
        stubs.sort(key=lambda s: s.last_update_time, reverse=True)
        stubs = stubs[:cap]

        out: list[dict[str, Any]] = []
        for stub in stubs:
            try:
                full = await session_service.get_session(
                    app_name=app_name, user_id=user_id, session_id=stub.id
                )
            except Exception:
                continue
            if full is None:
                continue
            out.append(
                {
                    "session_id": full.id,
                    "started_at": _started_at(full),
                    "title": _derive_title(full),
                }
            )
        return {
            "success": True,
            "mode": "index",
            "sessions": out,
            "count": len(out),
        }

    if session_id == current_session_id:
        return {
            "success": False,
            "error": "Refusing to read the current session; you already have its events in context.",
        }

    cap = min(max(limit or DEFAULT_EVENT_LIMIT, 1), MAX_EVENT_LIMIT)
    try:
        full = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except Exception as err:
        return {"success": False, "error": f"get_session failed: {err}"}
    if full is None:
        return {
            "success": False,
            "error": f"Session {session_id!r} not found for this user.",
        }

    events: list[dict[str, Any]] = []
    for event in full.events:
        text = _event_text(event)
        if text is None:
            continue
        events.append(
            {
                "author": _normalize_author(getattr(event, "author", None)),
                "text": text,
                "timestamp": float(getattr(event, "timestamp", 0.0) or 0.0),
            }
        )

    truncated = len(events) > cap
    if truncated:
        events = events[-cap:]

    return {
        "success": True,
        "mode": "drill",
        "session_id": full.id,
        "title": _derive_title(full),
        "events": events,
        "event_count": len(events),
        "truncated": truncated,
    }
