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

"""`look`: in-process read-only Calendar, Gmail, and Drive lookups rendered as answer cards."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.app_utils.http_client import default_http_transport
from app.auth.provider_hooks import provider_hooks
from app.auth.providers import app_for_server
from app.context_cards import card_items, record_context_card
from workspace_mcp import drive, gmail, google_calendar
from workspace_mcp._google import GoogleApi
from workspace_mcp._server import Server

logger = logging.getLogger(__name__)


class _Read(NamedTuple):
    server: Server
    tool: str
    kind: str
    title: str


_READS = {
    "calendar": _Read(
        google_calendar.server, "list_events", "events", "Calendar"
    ),
    "gmail": _Read(gmail.server, "search_threads", "threads", "Gmail"),
    "drive": _Read(drive.server, "search_files", "files", "Drive"),
}
_WINDOWS = ("today", "tomorrow", "week")
_WINDOW_LABELS = {"today": "today", "tomorrow": "tomorrow", "week": "this week"}
_SPOKEN = 3
_UNREAD = "is:unread"


class LookError(Exception):
    pass


async def look(app: str, query: str = "", window: str = "today") -> str:
    """Read-only quick lookup of Calendar, Gmail, or Drive shown as an answer card on screen.

    Args:
        app: 'calendar', 'gmail', or 'drive'.
        query: Search keywords or Gmail query ('from:sam'); empty lists events, unread mail, or recent files.
        window: Calendar window: 'today', 'tomorrow', or 'week'.
    """
    read = _READS.get(app.strip().lower())
    window = window.strip().lower() or "today"
    if read is None:
        return "look reads 'calendar', 'gmail', or 'drive'."
    if window not in _WINDOWS:
        return "window is 'today', 'tomorrow', or 'week'."
    query = query.strip()
    try:
        token = await _token(read.server)
        api = GoogleApi(token, transport=default_http_transport())
        try:
            args = await _arguments(read, api, query, window)
            reply = await _call(read.server, read.tool, args, api)
        finally:
            await api.aclose()
    except LookError as exc:
        return str(exc)
    items = card_items(read.kind, reply.get(read.kind) or [])
    lines = [_line(read.kind, item, window) for item in items]
    heading = _heading(read.kind, len(items), query, window)
    record_context_card(
        kind=read.kind,
        title=read.title,
        subtitle=heading,
        brand_icon=_brand_icon(read.server),
        bullets=lines[:_SPOKEN],
        surface_id=f"a2ui-ctx-{read.kind}",
        items=items,
        meta={
            "query": query,
            "window": window,
            "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    if not items:
        return f"{heading}."
    return f"{heading}: {', '.join(lines[:_SPOKEN])}."


async def _token(server: Server) -> str:
    app = app_for_server(server.name)
    token = (
        await provider_hooks(app.name).access_token(app.name) if app else None
    )
    if app is None or not token:
        name, app_id = (
            (app.display_name, app.name) if app else (server.name, server.name)
        )
        raise LookError(
            f"NOT CONNECTED: {name} (`{app_id}`). Say so in one short line and "
            f"call `navigate(target='app', app='{app_id}')` to open its Connect page."
        )
    return token


async def _call(
    server: Server, name: str, args: dict[str, Any], api: GoogleApi
) -> dict[str, Any]:
    tool = server.tools.get(name)
    if tool is None or tool.annotations.get("readOnlyHint") is not True:
        raise LookError(
            f"look only reads: {server.name}.{name} can change data."
        )
    result = await server.call_tool(name, args, api=api)
    if result.get("isError") or "structuredContent" not in result:
        text = "".join(c.get("text", "") for c in result.get("content") or [])
        logger.info("look: %s.%s failed: %s", server.name, name, text)
        raise LookError(
            f"{server.name} couldn't answer: {text.splitlines()[0] if text else 'no reply'}"
        )
    return result["structuredContent"]


async def _arguments(
    read: _Read, api: GoogleApi, query: str, window: str
) -> dict[str, Any]:
    if read.kind == "threads":
        return {"query": query or _UNREAD, "max_results": 10}
    args: dict[str, Any] = {"query": query} if query else {}
    if read.kind == "files" or window == "today":
        return args
    # Bare dates are read in the calendar's zone; a bare time_max includes that day.
    today = datetime.now(await _calendar_zone(read.server, api)).date()
    first, last = (
        (today + timedelta(days=1),) * 2
        if window == "tomorrow"
        else (today, today + timedelta(days=6))
    )
    return {**args, "time_min": first.isoformat(), "time_max": last.isoformat()}


async def _calendar_zone(server: Server, api: GoogleApi) -> tzinfo:
    reply = await _call(server, "list_calendars", {}, api)
    name = next(
        (
            c.get("time_zone")
            for c in reply.get("calendars") or []
            if c.get("primary")
        ),
        None,
    )
    try:
        return ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _brand_icon(server: Server) -> str:
    app = app_for_server(server.name)
    return app.name if app else server.name


def _heading(kind: str, count: int, query: str, window: str) -> str:
    if kind == "events":
        what = f" matching '{query}'" if query else ""
        noun = "event" if count == 1 else "events"
        return f"{count or 'No'} {noun}{what} {_WINDOW_LABELS[window]}"
    if kind == "threads":
        noun = "thread" if count == 1 else "threads"
        if query:
            return f"{count or 'No'} {noun} for '{query}'"
        return f"{count or 'No'} unread {noun}"
    noun = "file" if count == 1 else "files"
    return f"{count or 'No'} {noun}" + (f" for '{query}'" if query else "")


def _line(kind: str, item: dict[str, Any], window: str) -> str:
    if kind == "events":
        return f"{_when(item, window)} {item.get('summary') or '(no title)'}"
    if kind == "threads":
        sender = _sender(item.get("from", ""))
        subject = item.get("subject") or "(no subject)"
        return f"{sender}: {subject}" if sender else subject
    return item.get("name", "")


def _when(event: dict[str, Any], window: str) -> str:
    start = event.get("start", "")
    try:
        moment = (
            date.fromisoformat(start)
            if event.get("all_day")
            else datetime.fromisoformat(start)
        )
    except ValueError:
        return start
    day = f"{moment:%a} " if window == "week" else ""
    if isinstance(moment, datetime):
        return f"{day}{moment.hour}:{moment.minute:02d}"
    return f"{day}all day"


def _sender(value: str) -> str:
    name = value.split("<", 1)[0].strip().strip('"')
    return name or value.strip("<> ")
