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

"""The Google Calendar MCP server against a fake Calendar REST API."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.google_calendar import DESCRIPTION_LIMIT, server

_PREFIX = "/calendar/v3"

_EVENT = {
    "id": "ev1",
    "summary": "Standup",
    "start": {"dateTime": "2026-10-27T09:00:00-07:00"},
    "end": {"dateTime": "2026-10-27T09:15:00-07:00"},
    "location": "Room 1",
    "htmlLink": "https://calendar.google.com/event?eid=ev1",
    "attendees": [
        {"email": "ada@example.com", "responseStatus": "accepted"},
        {"email": "bob@example.com", "responseStatus": "needsAction"},
    ],
    "etag": "ignored",
}


class FakeCalendar:
    def __init__(
        self,
        routes: dict[tuple[str, str], Any],
        status: int = 200,
    ) -> None:
        self.routes, self.status = routes, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer fake-token"
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={"error": {"message": "Not Found", "errors": []}},
            )
        path = request.url.raw_path.decode().split("?")[0]
        key = (request.method, path.removeprefix(_PREFIX))
        assert key in self.routes, f"unexpected {key}"
        reply = self.routes[key]
        body = reply(request) if callable(reply) else reply
        return httpx.Response(200, json=body)

    def api(self) -> GoogleApi:
        return GoogleApi("fake-token", transport=httpx.MockTransport(self))

    def find(self, method: str, suffix: str) -> httpx.Request:
        for request in self.requests:
            path = request.url.raw_path.decode().split("?")[0]
            if request.method == method and path.endswith(suffix):
                return request
        raise AssertionError(f"no {method} ...{suffix}")


def _tz(zone: str = "America/Los_Angeles") -> dict[str, Any]:
    return {"id": "primary", "timeZone": zone}


def _text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_list_calendars() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/users/me/calendarList"): {
                "items": [
                    {
                        "id": "ada@example.com",
                        "summary": "Ada",
                        "primary": True,
                        "timeZone": "Europe/London",
                        "colorId": "3",
                    },
                    {
                        "id": "team@group.calendar.google.com",
                        "summary": "Team",
                        "summaryOverride": "My team",
                        "timeZone": "UTC",
                    },
                ]
            }
        }
    )
    result = await server.call_tool("list_calendars", {}, api=fake.api())
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "calendars": [
            {
                "id": "ada@example.com",
                "summary": "Ada",
                "primary": True,
                "time_zone": "Europe/London",
            },
            {
                "id": "team@group.calendar.google.com",
                "summary": "My team",
                "primary": False,
                "time_zone": "UTC",
            },
        ]
    }
    assert fake.requests[0].method == "GET"


@pytest.mark.asyncio
async def test_list_events_defaults_to_today_in_the_calendar_zone() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/calendars/primary"): _tz(),
            ("GET", "/calendars/primary/events"): {
                "items": [
                    _EVENT,
                    {
                        "id": "ev2",
                        "summary": "Holiday",
                        "start": {"date": "2026-10-27"},
                        "end": {"date": "2026-10-28"},
                    },
                ]
            },
        }
    )
    result = await server.call_tool("list_events", {}, api=fake.api())
    assert result["isError"] is False, _text(result)
    data = result["structuredContent"]
    tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(tz).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(
        today + timedelta(days=1), datetime.min.time(), tzinfo=tz
    )
    assert data["time_zone"] == "America/Los_Angeles"
    assert data["time_min"] == start.isoformat()
    assert data["time_max"] == end.isoformat()
    assert data["events"] == [
        {
            "id": "ev1",
            "summary": "Standup",
            "start": "2026-10-27T09:00:00-07:00",
            "end": "2026-10-27T09:15:00-07:00",
            "location": "Room 1",
            "attendees": [
                {"email": "ada@example.com", "response": "accepted"},
                {"email": "bob@example.com", "response": "needsAction"},
            ],
            "html_link": "https://calendar.google.com/event?eid=ev1",
            "all_day": False,
        },
        {
            "id": "ev2",
            "summary": "Holiday",
            "start": "2026-10-27",
            "end": "2026-10-28",
            "all_day": True,
        },
    ]
    params = fake.find("GET", "/events").url.params
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    assert params["maxResults"] == "25"
    assert params["timeMin"] == start.isoformat()
    assert params["timeMax"] == end.isoformat()
    assert "q" not in params


@pytest.mark.asyncio
async def test_list_events_reads_naive_times_and_dates_in_the_zone() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/calendars/team@group.calendar.google.com"): _tz(
                "Europe/Berlin"
            ),
            ("GET", "/calendars/team@group.calendar.google.com/events"): {
                "items": []
            },
        }
    )
    result = await server.call_tool(
        "list_events",
        {
            "calendar_id": "team@group.calendar.google.com",
            "time_min": "2026-07-01T08:00",
            "time_max": "2026-07-02",
            "query": "review",
            "max_results": 500,
        },
        api=fake.api(),
    )
    assert result["isError"] is False, _text(result)
    params = fake.find("GET", "/events").url.params
    assert params["timeMin"] == "2026-07-01T08:00:00+02:00"
    assert params["timeMax"] == "2026-07-03T00:00:00+02:00"
    assert params["q"] == "review"
    assert params["maxResults"] == "100"


@pytest.mark.asyncio
async def test_list_events_keeps_offsets_and_falls_back_to_utc() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/calendars/primary"): {"id": "primary"},
            ("GET", "/calendars/primary/events"): {"items": []},
        }
    )
    result = await server.call_tool(
        "list_events",
        {"time_min": "2026-07-01T08:00:00Z"},
        api=fake.api(),
    )
    data = result["structuredContent"]
    assert data["time_zone"] == "UTC"
    assert data["time_min"] == "2026-07-01T08:00:00+00:00"
    assert data["time_max"] == "2026-07-02T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"time_min": "next tuesday"}, "`time_min` should be ISO 8601"),
        (
            {"time_min": "2026-07-02", "time_max": "2026-07-01"},
            "`time_max` should be after `time_min`",
        ),
        ({"max_results": 0}, "`max_results` should be at least 1"),
        ({"max_results": "ten"}, "`max_results` should be a integer"),
    ],
)
async def test_list_events_rejects_bad_arguments(
    args: dict[str, Any], message: str
) -> None:
    fake = FakeCalendar({("GET", "/calendars/primary"): _tz()})
    result = await server.call_tool("list_events", args, api=fake.api())
    assert result["isError"] is True
    assert message in _text(result)
    assert not any(r.url.path.endswith("/events") for r in fake.requests)


@pytest.mark.asyncio
async def test_get_event_returns_a_truncated_text_description() -> None:
    long_html = "<p>Agenda</p>" + "x" * (DESCRIPTION_LIMIT + 50)
    fake = FakeCalendar(
        {
            ("GET", "/calendars/primary/events/ev1"): {
                **_EVENT,
                "description": long_html,
            }
        }
    )
    result = await server.call_tool(
        "get_event", {"event_id": "ev1"}, api=fake.api()
    )
    data = result["structuredContent"]
    assert data["id"] == "ev1"
    assert data["html_link"].endswith("eid=ev1")
    assert data["attendees"][0] == {
        "email": "ada@example.com",
        "response": "accepted",
    }
    assert data["description"].startswith("Agenda\n")
    assert len(data["description"]) == DESCRIPTION_LIMIT
    assert data["truncated"] is True
    assert "etag" not in data


@pytest.mark.asyncio
async def test_get_event_short_description_is_not_truncated() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/calendars/primary/events/ev1"): {
                **_EVENT,
                "description": "Bring a < b notes",
            }
        }
    )
    result = await server.call_tool(
        "get_event", {"event_id": "ev1"}, api=fake.api()
    )
    data = result["structuredContent"]
    assert data["description"] == "Bring a < b notes"
    assert "truncated" not in data


@pytest.mark.asyncio
async def test_get_event_not_found_is_a_hint() -> None:
    fake = FakeCalendar({}, status=404)
    result = await server.call_tool(
        "get_event", {"event_id": "nope"}, api=fake.api()
    )
    assert result["isError"] is True
    assert "not found: check the ID" in _text(result)


@pytest.mark.asyncio
async def test_get_event_needs_an_id() -> None:
    fake = FakeCalendar({})
    result = await server.call_tool("get_event", {}, api=fake.api())
    assert result["isError"] is True
    assert "missing required argument(s): event_id" in _text(result)
    assert fake.requests == []


def _echo(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    return {
        **body,
        "id": "new1",
        "htmlLink": "https://calendar.google.com/event?eid=new1",
        "attendees": [
            {**a, "responseStatus": "needsAction"}
            for a in body.get("attendees", [])
        ],
    }


@pytest.mark.asyncio
async def test_create_event_with_attendees_sends_invitations() -> None:
    fake = FakeCalendar(
        {
            ("GET", "/calendars/primary"): _tz(),
            ("POST", "/calendars/primary/events"): _echo,
        }
    )
    result = await server.call_tool(
        "create_event",
        {
            "summary": "1:1",
            "start": "2026-10-27T10:00",
            "end": "2026-10-27T10:30:00-07:00",
            "attendees": ["bob@example.com"],
            "description": "Catch up",
            "location": "Cafe",
        },
        api=fake.api(),
    )
    assert result["isError"] is False, _text(result)
    assert result["structuredContent"] == {
        "id": "new1",
        "html_link": "https://calendar.google.com/event?eid=new1",
        "start": "2026-10-27T10:00:00-07:00",
        "end": "2026-10-27T10:30:00-07:00",
        "attendees": [{"email": "bob@example.com", "response": "needsAction"}],
    }
    post = fake.find("POST", "/events")
    assert post.url.params["sendUpdates"] == "all"
    assert json.loads(post.content) == {
        "summary": "1:1",
        "start": {
            "dateTime": "2026-10-27T10:00:00-07:00",
            "timeZone": "America/Los_Angeles",
        },
        "end": {
            "dateTime": "2026-10-27T10:30:00-07:00",
            "timeZone": "America/Los_Angeles",
        },
        "description": "Catch up",
        "location": "Cafe",
        "attendees": [{"email": "bob@example.com"}],
    }


@pytest.mark.asyncio
async def test_create_all_day_event_without_attendees_notifies_no_one() -> None:
    fake = FakeCalendar({("POST", "/calendars/primary/events"): _echo})
    result = await server.call_tool(
        "create_event",
        {"summary": "Offsite", "start": "2026-11-02", "end": "2026-11-02"},
        api=fake.api(),
    )
    assert result["isError"] is False, _text(result)
    data = result["structuredContent"]
    assert data["start"] == "2026-11-02"
    assert data["end"] == "2026-11-03"
    assert "attendees" in data and data["attendees"] == []
    (post,) = fake.requests  # All-day events need no time zone lookup.
    assert post.url.params["sendUpdates"] == "none"
    body = json.loads(post.content)
    assert body["start"] == {"date": "2026-11-02"}
    assert body["end"] == {"date": "2026-11-03"}
    assert "attendees" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            {"start": "2026-10-27T10:00", "end": "2026-10-27T09:00"},
            "`end` should be after `start`",
        ),
        (
            {"start": "2026-10-28", "end": "2026-10-27"},
            "`end` should be after `start`",
        ),
        (
            {"start": "2026-10-27", "end": "2026-10-27T09:00"},
            "both be dates or both be datetimes",
        ),
        (
            {"start": "tomorrow 9am", "end": "2026-10-27T09:00"},
            "`start` should be ISO 8601",
        ),
        (
            {
                "start": "2026-10-27T09:00",
                "end": "2026-10-27T10:00",
                "attendees": ["bob"],
            },
            "`attendees` should be email addresses",
        ),
        ({"start": "2026-10-27T09:00"}, "missing required argument(s): end"),
    ],
)
async def test_create_event_rejects_bad_arguments(
    args: dict[str, Any], message: str
) -> None:
    fake = FakeCalendar({("GET", "/calendars/primary"): _tz()})
    result = await server.call_tool(
        "create_event", {"summary": "x", **args}, api=fake.api()
    )
    assert result["isError"] is True
    assert message in _text(result)
    assert not any(r.method == "POST" for r in fake.requests)


@pytest.mark.asyncio
async def test_create_event_forbidden_is_a_hint() -> None:
    fake = FakeCalendar({}, status=403)
    result = await server.call_tool(
        "create_event",
        {"summary": "x", "start": "2026-11-02", "end": "2026-11-03"},
        api=fake.api(),
    )
    assert result["isError"] is True
    assert "Google refused" in _text(result)


def test_annotations() -> None:
    hints = {n: t.annotations for n, t in server.tools.items()}
    assert list(hints) == [
        "list_calendars",
        "list_events",
        "get_event",
        "create_event",
    ]
    for name in ("list_calendars", "list_events", "get_event"):
        assert hints[name]["readOnlyHint"] is True
    assert hints["create_event"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }
