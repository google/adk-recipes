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

"""`look` reads one app in process, records an answer card, and says one short line."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app.app_utils.http_client import set_default_http_transport
from app.auth.providers import WORKSPACE_PROVIDER, record_provider_state
from app.context_cards import active_context_cards
from app.tools.look_tools import look
from workspace_mcp import google_calendar

Handler = Callable[[httpx.Request], httpx.Response]


class FakeGoogle:
    """Serves `routes` (path -> JSON or handler) and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            route = self.routes.get(request.url.path)
            if route is None:
                return httpx.Response(404, json={"error": {"message": "nope"}})
            return (
                route(request)
                if callable(route)
                else httpx.Response(200, json=route)
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGoogle]:
    monkeypatch.setenv("GOOGLE_WORKSPACE_ACCESS_TOKEN", "gw-look-token")
    record_provider_state(
        WORKSPACE_PROVIDER, "alex@example.com", expires_in=3600
    )
    fake = FakeGoogle()
    set_default_http_transport(fake.transport())
    yield fake
    set_default_http_transport(None)


def _card(kind: str) -> dict[str, Any]:
    return next(c for c in active_context_cards() if c["subkind"] == kind)


CALENDAR = "/calendar/v3/calendars/primary"


def _event(i: int, hour: int, summary: str) -> dict[str, Any]:
    return {
        "id": f"e{i}",
        "summary": summary,
        "start": {"dateTime": f"2026-10-27T{hour:02d}:00:00-07:00"},
        "end": {"dateTime": f"2026-10-27T{hour:02d}:30:00-07:00"},
        "description": "private agenda",
        "attendees": [
            {"email": "sam@example.com", "responseStatus": "accepted"}
        ],
    }


@pytest.mark.asyncio
async def test_calendar_today_records_events_and_says_the_top_three(
    google: FakeGoogle,
) -> None:
    google.routes[CALENDAR] = {"timeZone": "America/Los_Angeles"}
    google.routes[f"{CALENDAR}/events"] = {
        "items": [
            _event(1, 9, "Standup"),
            _event(2, 11, "Design review"),
            _event(3, 13, "Lunch with Sam"),
            _event(4, 16, "1:1"),
        ]
    }

    said = await look("calendar")

    assert said == (
        "4 events today: 9:00 Standup, 11:00 Design review, 13:00 Lunch with Sam."
    )
    events = google.requests[-1]
    assert events.headers["Authorization"] == "Bearer gw-look-token"
    # Today is the handler's default window: no list_calendars call first.
    assert all("calendarList" not in r.url.path for r in google.requests)
    card = _card("events")
    assert card["surfaceId"] == "a2ui-ctx-events"
    assert card["harness"] == "google_workspace"
    items = card["dataModel"]["context"]["items"]
    assert [i["id"] for i in items] == ["e1", "e2", "e3", "e4"]
    assert "description" not in items[0]
    assert items[0]["attendees"] == [
        {"email": "sam@example.com", "response": "accepted"}
    ]


@pytest.mark.asyncio
async def test_calendar_tomorrow_reads_the_primary_zone_and_asks_for_that_day(
    google: FakeGoogle,
) -> None:
    google.routes["/calendar/v3/users/me/calendarList"] = {
        "items": [{"id": "alex", "primary": True, "timeZone": "Asia/Tokyo"}]
    }
    google.routes[CALENDAR] = {"timeZone": "Asia/Tokyo"}
    google.routes[f"{CALENDAR}/events"] = {"items": []}

    said = await look("calendar", window="tomorrow")

    assert said == "No events tomorrow."
    params = google.requests[-1].url.params
    # Bare dates in Tokyo: the whole of tomorrow there.
    assert params["timeMin"].endswith("T00:00:00+09:00")
    assert params["timeMax"].endswith("T00:00:00+09:00")
    assert params["timeMin"] < params["timeMax"]


@pytest.mark.asyncio
async def test_gmail_lists_unread_threads_by_default(
    google: FakeGoogle,
) -> None:
    google.routes["/gmail/v1/users/me/threads"] = {"threads": [{"id": "t1"}]}
    google.routes["/gmail/v1/users/me/threads/t1"] = {
        "snippet": "Are we still on for Friday?",
        "messages": [
            {
                "labelIds": ["UNREAD"],
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Friday"},
                        {
                            "name": "From",
                            "value": '"Sam Lee" <sam@example.com>',
                        },
                    ]
                },
            }
        ],
    }

    said = await look("gmail")

    assert said == "1 unread thread: Sam Lee: Friday."
    assert google.requests[0].url.params["q"] == "is:unread"
    (item,) = _card("threads")["dataModel"]["context"]["items"]
    assert item["unread"] is True and item["message_count"] == 1


@pytest.mark.asyncio
async def test_drive_searches_files(google: FakeGoogle) -> None:
    google.routes["/drive/v3/files"] = {
        "files": [
            {
                "id": "f1",
                "name": "Q4 deck",
                "mimeType": "application/vnd.google-apps.presentation",
                "webViewLink": "https://docs.google.com/presentation/d/f1",
                "owners": [{"displayName": "Alex"}],
            }
        ]
    }

    said = await look("drive", query="Q4 deck")

    assert said == "1 file for 'Q4 deck': Q4 deck."
    assert "'Q4 deck'" in google.requests[0].url.params["q"]
    (item,) = _card("files")["dataModel"]["context"]["items"]
    assert set(item) == {"id", "name", "mime_type", "web_view_link"}


@pytest.mark.asyncio
async def test_not_connected_says_how_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGoogle()
    set_default_http_transport(fake.transport())
    try:
        said = await look("gmail")
    finally:
        set_default_http_transport(None)

    assert said.startswith("NOT CONNECTED: Google (`google_workspace`)")
    assert "navigate(target='app', app='google_workspace')" in said
    assert fake.requests == []


@pytest.mark.asyncio
async def test_a_tool_that_is_not_read_only_is_refused_without_a_call(
    google: FakeGoogle, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = google_calendar.server.tools["list_events"]
    monkeypatch.setitem(
        google_calendar.server.tools,
        "list_events",
        dataclasses.replace(tool, annotations={"readOnlyHint": False}),
    )

    said = await look("calendar")

    assert (
        said == "look only reads: google_calendar.list_events can change data."
    )
    assert google.requests == []
    assert active_context_cards() == []


@pytest.mark.asyncio
async def test_a_google_error_comes_back_as_its_one_line_hint(
    google: FakeGoogle,
) -> None:
    google.routes["/drive/v3/files"] = lambda _: httpx.Response(
        403,
        json={
            "error": {
                "message": "Drive API has not been used",
                "errors": [{"reason": "accessNotConfigured"}],
            }
        },
    )

    said = await look("drive", query="budget")

    assert said.startswith("google_drive couldn't answer: this Google API is")
    assert "make onboard" in said and "\n" not in said


@pytest.mark.asyncio
async def test_unknown_app_or_window_is_refused(google: FakeGoogle) -> None:
    assert "calendar" in await look("slack")
    assert "tomorrow" in await look("calendar", window="month")
    assert google.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app", "kind"),
    [("calendar", "events"), ("gmail", "threads"), ("drive", "files")],
)
async def test_the_eval_fakes_serve_every_look(
    google: FakeGoogle, app: str, kind: str
) -> None:
    """System 1 runs `look` against `tests/fake_integrations.py`: each app has items."""
    from tests.fake_integrations import integrations_transport

    set_default_http_transport(integrations_transport())
    await look(app)
    assert _card(kind)["dataModel"]["context"]["items"], app
