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

"""Answer cards (`card_items` and `voice_agent.present_card`): typed fields, caps, and persistence."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.api import mcp_gateway
from app.api.mcp import (
    MCP_TOOLS,
    McpTokenGrant,
    clear_mcp_tokens,
    mint_mcp_token,
)
from app.api.serializers import thread_detail, thread_summary
from app.api.surfaces import build_a2ui_surfaces
from app.auth.identity import run_as
from app.context_cards import (
    CARD_KINDS,
    MAX_CARD_ITEMS,
    MAX_CARD_TEXT,
    card_items,
)
from app.fast_api_app import app
from app.store.thread_store import ThreadStore
from app.threads.model import ThreadHandle, ThreadState
from app.threads.registry import (
    MAX_THREAD_CARDS,
    ThreadRegistry,
    set_thread_registry,
)

WEB_CARDS = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "cards.ts"
)

EVENTS = [
    {
        "id": "ev1",
        "summary": "Standup",
        "start": "2026-10-27T09:00:00-07:00",
        "end": "2026-10-27T09:15:00-07:00",
        "all_day": False,
        "attendees": [
            {"email": "ada@example.com", "response": "accepted", "self": True}
        ],
        "description": "Private agenda the card must not keep.",
        "html_link": "https://calendar.google.com/event?eid=ev1",
    },
    {"id": "ev2", "summary": "1:1", "start": "2026-10-27T11:00:00-07:00"},
]


@pytest.fixture(autouse=True)
def _no_upstreams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setattr(mcp_gateway, "MCP_JSON_PATH", tmp_path / "none.json")
    clear_mcp_tokens()
    yield
    clear_mcp_tokens()
    set_thread_registry(None)


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[ThreadStore]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'threads.db'}"
    )
    yield ThreadStore(engine)
    await engine.dispose()


def _thread(
    thread_id: str = "thread-card0001",
    state: ThreadState = ThreadState.EXECUTING,
) -> ThreadHandle:
    return ThreadHandle(
        thread_id=thread_id,
        goal="What's on my calendar tomorrow?",
        project="scratch/2026-10-26-calendar",
        harness="claude",
        branch="",
        owner="local",
        state=state,
    )


async def _present(thread_id: str, **args: Any) -> Any:
    spec = next(t for t in MCP_TOOLS if t.name == "present_card")
    grant = McpTokenGrant(token="t", thread_id=thread_id, user_id="local")
    with run_as("local"):
        return await spec.handler(grant, args)


def test_events_keep_their_fields_and_drop_the_rest() -> None:
    raw = {
        "id": "e1",
        "summary": "Standup",
        "start": "2026-10-26T09:00:00-07:00",
        "end": "2026-10-26T09:15:00-07:00",
        "all_day": False,
        "location": "Room 4",
        "attendees": [
            {"email": "sam@example.com", "response": "accepted", "self": True},
            {"response": "declined"},  # no email: dropped
            "nobody",
        ],
        "html_link": "https://calendar.google.com/e1",
        "description": "secret agenda",
    }
    (item,) = card_items("events", [raw])
    assert item == {
        "id": "e1",
        "summary": "Standup",
        "start": "2026-10-26T09:00:00-07:00",
        "end": "2026-10-26T09:15:00-07:00",
        "all_day": False,
        "location": "Room 4",
        "attendees": [{"email": "sam@example.com", "response": "accepted"}],
        "html_link": "https://calendar.google.com/e1",
    }


def test_threads_and_files_keep_typed_fields_only() -> None:
    (thread,) = card_items(
        "threads",
        [
            {
                "id": "t1",
                "subject": "Q4",
                "unread": "yes",  # not a bool: dropped
                "message_count": True,  # a bool is not a count
                "body": "the whole email",
            }
        ],
    )
    assert thread == {"id": "t1", "subject": "Q4"}
    (file,) = card_items(
        "files",
        [
            {
                "id": "f1",
                "name": "Deck",
                "web_view_link": "https://x",
                "text": "…",
            }
        ],
    )
    assert file == {"id": "f1", "name": "Deck", "web_view_link": "https://x"}


def test_items_are_capped_and_long_text_is_cut() -> None:
    raw = [{"id": str(i), "snippet": "x" * 1000} for i in range(50)]
    items = card_items("threads", raw)
    assert len(items) == MAX_CARD_ITEMS
    assert len(items[0]["snippet"]) == MAX_CARD_TEXT


def test_items_without_an_id_are_dropped() -> None:
    assert card_items("files", [{"name": "x"}, "x", {"id": ""}]) == []


def test_non_https_link_fields_are_dropped() -> None:
    events = card_items(
        "events",
        [
            {"id": "e1", "html_link": "javascript:alert(1)"},
            {"id": "e2", "html_link": "http://calendar.google.com/e2"},
            {"id": "e3", "html_link": "HTTPS://calendar.google.com/e3"},
        ],
    )
    assert events == [
        {"id": "e1"},
        {"id": "e2"},
        {"id": "e3", "html_link": "HTTPS://calendar.google.com/e3"},
    ]
    files = card_items(
        "files",
        [
            {"id": "f1", "web_view_link": "data:text/html,<script>"},
            {"id": "f2", "web_view_link": "https://drive.google.com/file/d/f2"},
        ],
    )
    assert files == [
        {"id": "f1"},
        {"id": "f2", "web_view_link": "https://drive.google.com/file/d/f2"},
    ]


@pytest.mark.parametrize(
    ("kind", "items", "error"),
    [
        ("notes", [], "unknown card kind 'notes'"),
        ("events", {"id": "e1"}, "items should be a list"),
    ],
)
def test_bad_kinds_and_items_are_rejected(
    kind: str, items: object, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        card_items(kind, items)


def test_the_web_types_name_the_same_kinds_and_fields() -> None:
    source = WEB_CARDS.read_text()
    kinds = re.search(r"CARD_KINDS = \[(.*?)\]", source)
    assert kinds is not None
    assert re.findall(r'"(\w+)"', kinds.group(1)) == list(CARD_KINDS)
    interfaces = {
        "events": "EventItem",
        "threads": "ThreadItem",
        "files": "FileItem",
    }
    for kind, name in interfaces.items():
        body = re.search(rf"interface {name} \{{(.*?)\}}", source, re.S)
        assert body is not None, name
        fields = re.findall(r"^\s*(\w+)\??:", body.group(1), re.M)
        assert fields == list(CARD_KINDS[kind]), kind


# ---- present_card tool ------------------------------------------------------


def test_the_tool_is_listed_with_its_schema() -> None:
    spec = next(t for t in MCP_TOOLS if t.name == "present_card").to_mcp_dict()
    assert spec["description"].startswith("Call once at the end")
    props = spec["inputSchema"]["properties"]
    assert props["kind"]["enum"] == ["events", "threads", "files"]
    assert props["items"] == {
        "type": "array",
        "items": {"type": "object"},
        "description": props["items"]["description"],
    }
    assert spec["inputSchema"]["required"] == ["kind", "title", "items"]


@pytest.mark.asyncio
async def test_cards_are_trimmed_stored_and_survive_a_restart(
    store: ThreadStore,
):
    registry = ThreadRegistry(store)
    set_thread_registry(registry)
    handle = _thread()
    registry._threads[handle.thread_id] = handle
    await registry.save(handle)

    reply = await _present(
        handle.thread_id, kind="events", title=" Tomorrow ", items=EVENTS
    )
    assert reply == "Card shown: 2 events."
    (card,) = handle.cards
    assert card["kind"] == "events" and card["title"] == "Tomorrow"
    first = card["items"][0]
    assert "description" not in first, "fields outside the allowed set drop"
    assert first["attendees"] == [
        {"email": "ada@example.com", "response": "accepted"}
    ]
    assert card["items"][1] == {
        "id": "ev2",
        "summary": "1:1",
        "start": "2026-10-27T11:00:00-07:00",
    }

    # A server restart: the card comes back from the store with the thread.
    with run_as("local"):
        reloaded = await ThreadRegistry(store).require(handle.thread_id)
    assert reloaded.cards == handle.cards
    assert ThreadHandle.from_dict(handle.to_dict()).cards == handle.cards


def test_threads_without_cards_keep_their_snapshot() -> None:
    assert "cards" not in _thread().to_dict()
    assert ThreadHandle.from_dict(_thread().to_dict()).cards == []


@pytest.mark.asyncio
async def test_unknown_kind_empty_items_and_the_cap_are_errors():
    registry = ThreadRegistry()
    set_thread_registry(registry)
    handle = _thread()
    registry._threads[handle.thread_id] = handle

    with pytest.raises(ValueError, match="unknown card kind 'sheets'"):
        await _present(handle.thread_id, kind="sheets", title="x", items=EVENTS)
    with pytest.raises(ValueError, match="exactly as the Google tools"):
        await _present(
            handle.thread_id,
            kind="threads",
            title="x",
            items=[{"subject": "hi"}],
        )
    for _ in range(MAX_THREAD_CARDS):
        await _present(handle.thread_id, kind="events", title="x", items=EVENTS)
    with pytest.raises(ValueError, match="already shows 3 cards"):
        await _present(handle.thread_id, kind="events", title="x", items=EVENTS)
    assert len(handle.cards) == MAX_THREAD_CARDS


def test_errors_reach_the_computer_as_tool_error_text() -> None:
    registry = ThreadRegistry()
    set_thread_registry(registry)
    handle = _thread()
    registry._threads[handle.thread_id] = handle
    token = mint_mcp_token(handle.thread_id, "local")

    res = TestClient(app).post(
        "/api/v1/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "present_card",
                "arguments": {"kind": "slides", "title": "x", "items": []},
            },
        },
    )
    result = res.json()["result"]
    assert result["isError"] is True
    assert "unknown card kind" in result["content"][0]["text"]


def test_summary_and_outcome_serve_cards_and_as_of() -> None:
    handle = _thread(state=ThreadState.COMPLETED)
    handle.result_text = "You have two meetings tomorrow."
    handle.ended_at = float(int(time.time()))
    handle.cards = [
        {"kind": "events", "title": "Tomorrow", "items": [{"id": "ev2"}]}
    ]

    summary = thread_summary(handle)
    detail = thread_detail(handle)
    assert summary["cards"] == handle.cards
    assert detail["turns"][-1]["cards"] == handle.cards
    # Coding threads render in ThreadView; build_a2ui_surfaces returns only context/answer cards.
    assert build_a2ui_surfaces() == []
