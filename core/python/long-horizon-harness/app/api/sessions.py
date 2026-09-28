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

"""REST routes that expose the chat UI's session list metadata.

The web UI uses these to render the left-side chat history sidebar (list /
rename / delete). Per-chat history is sourced from the A2A ``tasks/get`` API
plus the ``/lha/contexts/{id}/tasks`` index (see app/api/tasks.py).
All routes are keyed by the authenticated user_id (see ``app/auth/identity.py``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from google.adk.agents.invocation_context import new_invocation_context_id
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import GetSessionConfig
from pydantic import BaseModel, Field

from app.auth import current_user_id
from app.infrastructure.constants import (
    APP_NAME,
    SCHEDULER_JOB_KEY,
    SCHEDULER_SOURCE,
    SESSION_SOURCE_KEY,
    TITLE_KEY,
    TITLE_MAX_LEN,
)
from app.workspace_window import WORKSPACE_WINDOW_STATE_KEY, window_dirs

logger = logging.getLogger(__name__)

# Re-exported so importers (and tests) can keep using
# `from app.api.sessions import TITLE_KEY`; canonical in constants.
__all__ = ["TITLE_KEY", "TITLE_MAX_LEN", "attach_session_routes"]

# The unfiltered list scans the 50 most-recent stubs (one get_session each).
# Any filtered request scans deeper so a busy chat history doesn't bury
# matching rows; both scan-cap and page-cap truncation are logged.
SESSIONS_PAGE_SIZE = 50
SCHEDULER_SCAN_LIMIT = 200
# The list view needs only state (title/source/jobType) plus the last event
# timestamp. Loading the full event history per row is the 502 driver, so the
# primary fetch is bounded to a small RECENT window (num_recent_events). The
# title comes from state (TITLE_KEY) — set by the rename endpoint and by
# session-start for new chats — so the recent window does not need to carry the
# first user message. last_update_time comes from the session row, not events.
SESSIONS_EVENT_FETCH = 5


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LEN)


class WindowBody(BaseModel):
    dirs: list[str] = Field(default_factory=list)


def _clean_window(raw: list[str]) -> list[str]:
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        cleaned = entry.strip().strip("/")
        if cleaned and cleaned != ".":
            out.append(cleaned)
    return out


def _explicit_title(session: Any) -> str | None:
    explicit = session.state.get(TITLE_KEY)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return None


def first_user_message_title(session: Any) -> str | None:
    # Scan in chronological (oldest-first) order so the title is the FIRST user
    # message, not a recent one. Callers must pass a session whose events
    # include the start of the conversation (a recent-only window will pick the
    # wrong message — see SESSIONS_EVENT_FETCH).
    for event in session.events:
        if getattr(event, "author", None) != "user":
            continue
        content = getattr(event, "content", None)
        if not content or not content.parts:
            continue
        for part in content.parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                snippet = text.strip().splitlines()[0]
                return snippet[:TITLE_MAX_LEN]
    return None


def _created_at(session: Any) -> float:
    for event in session.events:
        ts = getattr(event, "timestamp", None)
        if isinstance(ts, (int, float)):
            return float(ts)
    return float(session.last_update_time)


def _source(session: Any) -> str:
    value = session.state.get(SESSION_SOURCE_KEY)
    return value if value == SCHEDULER_SOURCE else "user"


def _job_type(session: Any) -> str | None:
    value = session.state.get(SCHEDULER_JOB_KEY)
    return value if isinstance(value, str) and value else None


async def _persist_title(
    runner: Runner, user_id: str, session: Any, title: str
) -> None:
    # Write the derived title back to state so subsequent list loads read it
    # cheaply from the recent-events window instead of re-fetching full history.
    try:
        await runner.session_service.append_event(
            session,
            Event(
                invocation_id=new_invocation_context_id(),
                author="system",
                actions=EventActions(state_delta={TITLE_KEY: title}),
            ),
        )
    except Exception:
        logger.exception(
            "lha_sessions: failed to persist derived title for %s/%s",
            user_id,
            getattr(session, "id", "?"),
        )


async def _resolve_title_and_created(
    *, runner: Runner, user_id: str, recent: Any
) -> tuple[str, float]:
    # Fast path: an explicit title (rename, or session-start for new chats) is in
    # state, returned regardless of the recent-events window.
    explicit = _explicit_title(recent)
    if explicit is not None:
        return explicit, _created_at(recent)

    # No stored title: the recent-events window keeps the MOST RECENT events, so
    # it cannot supply the FIRST user message. Re-fetch the oldest events once
    # to derive the true title (and created time), then persist it so this row
    # is cheap forever after — a one-time lazy migration for legacy chats.
    try:
        full = await runner.session_service.get_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=recent.id,
            config=GetSessionConfig(num_recent_events=None),
        )
    except Exception:
        logger.exception(
            "lha_sessions: oldest-events fetch failed for %s/%s",
            user_id,
            recent.id,
        )
        full = None

    source = full if full is not None else recent
    title = first_user_message_title(source) or "New chat"
    created_at = _created_at(source)
    if full is not None and title != "New chat":
        await _persist_title(runner, user_id, full, title)
    return title, created_at


def attach_session_routes(app: FastAPI, *, runner: Runner) -> None:
    router = APIRouter(prefix="/lha/sessions", tags=["lha"])

    @router.get("")
    async def list_chats(
        user_id: str = Depends(current_user_id),
        source: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            resp = await runner.session_service.list_sessions(
                app_name=APP_NAME, user_id=user_id
            )
        except Exception as err:
            logger.exception(
                "lha_sessions: list_sessions failed for %s", user_id
            )
            raise HTTPException(
                status_code=500, detail="list sessions failed"
            ) from err

        # list_sessions returns stubs WITHOUT state, so we need a get_session
        # per row to read the source tag + title. The main list scans the 50
        # most-recent; the scheduler folder scans deeper (still bounded).
        ordered = sorted(
            resp.sessions, key=lambda s: s.last_update_time, reverse=True
        )
        scan_limit = (
            SESSIONS_PAGE_SIZE if source is None else SCHEDULER_SCAN_LIMIT
        )
        scanned = ordered[:scan_limit]
        needle = q.strip().lower() if q and q.strip() else None

        out: list[dict[str, Any]] = []
        truncated_by_output = False
        for stub in scanned:
            try:
                full = await runner.session_service.get_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=stub.id,
                    config=GetSessionConfig(
                        num_recent_events=SESSIONS_EVENT_FETCH
                    ),
                )
            except Exception:
                logger.exception(
                    "lha_sessions: get_session failed for %s/%s",
                    user_id,
                    stub.id,
                )
                continue
            if full is None:
                continue
            src = _source(full)
            if source == SCHEDULER_SOURCE and src != SCHEDULER_SOURCE:
                continue
            if source == "user" and src == SCHEDULER_SOURCE:
                continue
            title, created_at = await _resolve_title_and_created(
                runner=runner, user_id=user_id, recent=full
            )
            if needle and needle not in title.lower():
                continue
            out.append(
                {
                    "id": full.id,
                    "title": title,
                    "createdAt": created_at,
                    "lastUpdated": float(full.last_update_time),
                    "source": src,
                    "jobType": _job_type(full),
                    "workspaceWindow": window_dirs(
                        getattr(full, "state", None)
                    ),
                }
            )
            if len(out) >= SESSIONS_PAGE_SIZE:
                truncated_by_output = True
                break

        if truncated_by_output or len(ordered) > scan_limit:
            logger.info(
                "lha_sessions: result truncated for source=%s — older rows not shown "
                "(scanned %d of %d)",
                source or "all",
                len(scanned),
                len(ordered),
            )
        return out

    @router.patch("/{session_id}", status_code=204)
    async def rename(
        session_id: str,
        body: RenameBody,
        user_id: str = Depends(current_user_id),
    ) -> None:
        try:
            session = await runner.session_service.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        except Exception as err:
            logger.exception("lha_sessions: get_session failed for rename")
            raise HTTPException(
                status_code=500, detail="session lookup failed"
            ) from err
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

        try:
            await runner.session_service.append_event(
                session,
                Event(
                    invocation_id=new_invocation_context_id(),
                    author="system",
                    actions=EventActions(
                        state_delta={TITLE_KEY: body.title.strip()}
                    ),
                ),
            )
        except Exception as err:
            logger.exception("lha_sessions: append_event failed for rename")
            raise HTTPException(
                status_code=500, detail="rename failed"
            ) from err

    @router.put("/{session_id}/window", status_code=204)
    async def set_window(
        session_id: str,
        body: WindowBody,
        user_id: str = Depends(current_user_id),
    ) -> None:
        # A chat's project = its workspace_window anchor. Create-or-update so
        # the web can seed a new project-chat before its first turn.
        dirs = _clean_window(body.dirs)
        try:
            session = await runner.session_service.get_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        except Exception as err:
            logger.exception("lha_sessions: get_session failed for set_window")
            raise HTTPException(
                status_code=500, detail="session lookup failed"
            ) from err
        try:
            if session is None:
                await runner.session_service.create_session(
                    app_name=APP_NAME,
                    user_id=user_id,
                    session_id=session_id,
                    state={WORKSPACE_WINDOW_STATE_KEY: dirs},
                )
            else:
                await runner.session_service.append_event(
                    session,
                    Event(
                        invocation_id=new_invocation_context_id(),
                        author="system",
                        actions=EventActions(
                            state_delta={WORKSPACE_WINDOW_STATE_KEY: dirs}
                        ),
                    ),
                )
        except Exception as err:
            logger.exception("lha_sessions: set_window failed")
            raise HTTPException(
                status_code=500, detail="set window failed"
            ) from err

    @router.delete("/{session_id}", status_code=204)
    async def delete_chat(
        session_id: str,
        user_id: str = Depends(current_user_id),
    ) -> None:
        try:
            await runner.session_service.delete_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        except Exception as err:
            logger.exception("lha_sessions: delete_session failed")
            raise HTTPException(
                status_code=500, detail="delete failed"
            ) from err

    @router.delete("")
    async def delete_all_chats(
        user_id: str = Depends(current_user_id),
    ) -> dict[str, int]:
        try:
            resp = await runner.session_service.list_sessions(
                app_name=APP_NAME, user_id=user_id
            )
        except Exception as err:
            logger.exception("lha_sessions: list_sessions failed (bulk delete)")
            raise HTTPException(
                status_code=500, detail="bulk delete failed: list"
            ) from err

        deleted = 0
        failed = 0
        for stub in resp.sessions:
            try:
                await runner.session_service.delete_session(
                    app_name=APP_NAME, user_id=user_id, session_id=stub.id
                )
                deleted += 1
            except Exception:
                logger.exception(
                    "lha_sessions: delete_session failed for %s/%s in bulk",
                    user_id,
                    stub.id,
                )
                failed += 1
        return {"deleted": deleted, "failed": failed}

    app.include_router(router)
