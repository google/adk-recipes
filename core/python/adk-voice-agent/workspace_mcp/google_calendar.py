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

"""Google Calendar MCP tools (`python -m workspace_mcp.google_calendar`)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workspace_mcp._google import GoogleApi, html_to_text, truncate
from workspace_mcp._server import Server

BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_MAX_RESULTS = 25
MAX_RESULTS_CAP = 100
DESCRIPTION_LIMIT = 2000
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")

server = Server("google_calendar")

_CALENDAR_ID = {
    "type": "string",
    "description": "Calendar ID from list_calendars (default: primary).",
}


def _cal_url(calendar_id: str, *rest: str) -> str:
    parts = [quote(calendar_id, safe="@.")]
    parts += [quote(p, safe="") for p in rest]
    return f"{BASE}/calendars/" + "/".join(parts)


def _calendar_id(args: dict[str, Any]) -> str:
    return str(args.get("calendar_id") or "primary").strip() or "primary"


def _zone(name: str | None) -> tuple[str, tzinfo]:
    if name:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC", UTC


async def _calendar_zone(
    api: GoogleApi, calendar_id: str
) -> tuple[str, tzinfo]:
    calendar = await api.get(_cal_url(calendar_id))
    return _zone(calendar.get("timeZone"))


def _parse(field: str, value: Any, tz: tzinfo) -> date | datetime:
    text = str(value).strip()
    if _DATE_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed
    raise ValueError(
        f"`{field}` should be ISO 8601 (2026-10-27 or 2026-10-27T09:00), not {text!r}"
    )


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(0), tzinfo=tz)


def _rfc3339(moment: date) -> str:
    if isinstance(moment, datetime):
        return moment.isoformat(timespec="seconds")
    return moment.isoformat()


def _when(value: dict[str, Any] | None) -> str | None:
    value = value or {}
    return value.get("dateTime") or value.get("date")


def _attendees(event: dict[str, Any]) -> list[dict[str, str]]:
    people = []
    for person in event.get("attendees") or []:
        entry = {
            "email": person.get("email"),
            "response": person.get("responseStatus"),
        }
        people.append({k: v for k, v in entry.items() if v})
    return people


def _event(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") or {}
    shaped = {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "start": _when(start),
        "end": _when(event.get("end")),
        "location": event.get("location"),
        "attendees": _attendees(event),
        "html_link": event.get("htmlLink"),
        "all_day": "date" in start and "dateTime" not in start,
    }
    return {k: v for k, v in shaped.items() if v not in (None, "", [])}


@server.tool(
    "list_calendars",
    "List the user's calendars with their IDs and time zones.",
    {"type": "object", "properties": {}, "required": []},
    read_only=True,
)
async def list_calendars(
    api: GoogleApi, args: dict[str, Any]
) -> dict[str, Any]:
    reply = await api.get(
        f"{BASE}/users/me/calendarList", params={"maxResults": 250}
    )
    calendars = []
    for item in reply.get("items") or []:
        calendars.append(
            {
                "id": item.get("id"),
                "summary": item.get("summaryOverride") or item.get("summary"),
                "primary": bool(item.get("primary")),
                "time_zone": item.get("timeZone"),
            }
        )
    return {"calendars": calendars}


@server.tool(
    "list_events",
    "List events in a time window (default: today in the calendar's time "
    "zone), soonest first.",
    {
        "type": "object",
        "properties": {
            "calendar_id": _CALENDAR_ID,
            "time_min": {
                "type": "string",
                "description": "Window start, ISO 8601; no offset means the "
                "calendar's time zone (default: start of today).",
            },
            "time_max": {
                "type": "string",
                "description": "Window end, ISO 8601; a bare date includes "
                "that whole day (default: end of time_min's day).",
            },
            "query": {
                "type": "string",
                "description": "Free-text search in title, description, "
                "location and attendees.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Most events to return (default "
                f"{DEFAULT_MAX_RESULTS}, max {MAX_RESULTS_CAP}).",
            },
        },
        "required": [],
    },
    read_only=True,
)
async def list_events(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    max_results = args.get("max_results")
    if max_results is None:
        max_results = DEFAULT_MAX_RESULTS
    if max_results < 1:
        raise ValueError("`max_results` should be at least 1")
    max_results = min(max_results, MAX_RESULTS_CAP)
    calendar_id = _calendar_id(args)
    zone_name, tz = await _calendar_zone(api, calendar_id)

    if args.get("time_min"):
        start = _parse("time_min", args["time_min"], tz)
        if not isinstance(start, datetime):
            start = _midnight(start, tz)
    else:
        start = _midnight(datetime.now(tz).date(), tz)
    if args.get("time_max"):
        end = _parse("time_max", args["time_max"], tz)
        if not isinstance(end, datetime):
            end = _midnight(end + timedelta(days=1), tz)
    else:
        day = start.astimezone(tz).date()
        end = _midnight(day + timedelta(days=1), tz)
    if end <= start:
        raise ValueError("`time_max` should be after `time_min`")

    params: dict[str, Any] = {
        "timeMin": _rfc3339(start),
        "timeMax": _rfc3339(end),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max_results,
        "timeZone": zone_name,
    }
    if args.get("query"):
        params["q"] = args["query"]
    reply = await api.get(_cal_url(calendar_id, "events"), params=params)
    return {
        "time_zone": zone_name,
        "time_min": params["timeMin"],
        "time_max": params["timeMax"],
        "events": [_event(e) for e in reply.get("items") or []],
    }


@server.tool(
    "get_event",
    "Read one event, including its description and attendees' responses.",
    {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Event ID from list_events.",
            },
            "calendar_id": _CALENDAR_ID,
        },
        "required": ["event_id"],
    },
    read_only=True,
)
async def get_event(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    event_id = str(args["event_id"]).strip()
    event = await api.get(_cal_url(_calendar_id(args), "events", event_id))
    shaped = _event(event)
    description = event.get("description") or ""
    if _TAG_RE.search(description):
        description = html_to_text(description)
    if description:
        text, cut = truncate(description, DESCRIPTION_LIMIT)
        shaped["description"] = text
        if cut:
            shaped["truncated"] = True
    return shaped


@server.tool(
    "create_event",
    "Create an event and email invitations to any attendees. A bare date "
    "(YYYY-MM-DD) makes an all-day event.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "start": {
                "type": "string",
                "description": "ISO 8601 date or datetime; no offset means "
                "the calendar's time zone.",
            },
            "end": {
                "type": "string",
                "description": "ISO 8601, same kind as start. For all-day "
                "events the end date is exclusive.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Email addresses to invite.",
            },
            "description": {"type": "string", "description": "Event notes."},
            "location": {"type": "string", "description": "Where it is."},
            "calendar_id": _CALENDAR_ID,
        },
        "required": ["summary", "start", "end"],
    },
    open_world=True,
)
async def create_event(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    attendees = args.get("attendees") or []
    for email in attendees:
        if not isinstance(email, str) or "@" not in email:
            raise ValueError(
                f"`attendees` should be email addresses, not {email!r}"
            )
    calendar_id = _calendar_id(args)
    dates = [
        bool(_DATE_RE.fullmatch(str(args[k]).strip())) for k in ("start", "end")
    ]
    if dates[0] != dates[1]:
        raise ValueError(
            "`start` and `end` should both be dates or both be datetimes"
        )
    body: dict[str, Any] = {"summary": args["summary"]}
    if dates[0]:
        start = _parse("start", args["start"], UTC)
        end = _parse("end", args["end"], UTC)
        if end < start:
            raise ValueError("`end` should be after `start`")
        if end == start:  # One all-day event: Google's end date is exclusive.
            end = start + timedelta(days=1)
        body["start"] = {"date": start.isoformat()}
        body["end"] = {"date": end.isoformat()}
    else:
        zone_name, tz = await _calendar_zone(api, calendar_id)
        start = _parse("start", args["start"], tz)
        end = _parse("end", args["end"], tz)
        if end <= start:
            raise ValueError("`end` should be after `start`")
        body["start"] = {"dateTime": _rfc3339(start), "timeZone": zone_name}
        body["end"] = {"dateTime": _rfc3339(end), "timeZone": zone_name}
    for key in ("description", "location"):
        if args.get(key):
            body[key] = args[key]
    if attendees:
        body["attendees"] = [{"email": e.strip()} for e in attendees]
    created = await api.post(
        _cal_url(calendar_id, "events"),
        params={"sendUpdates": "all" if attendees else "none"},
        json=body,
    )
    shaped = {
        "id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "start": _when(created.get("start")),
        "end": _when(created.get("end")),
        "attendees": _attendees(created),
    }
    return {k: v for k, v in shaped.items() if v not in (None, "")}


if __name__ == "__main__":
    server.run()
