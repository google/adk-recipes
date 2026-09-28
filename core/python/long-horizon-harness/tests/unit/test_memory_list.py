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

"""Tests for app.memory.memory_list — cross-session memory listing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from google.adk.events import Event
from google.adk.memory import InMemoryMemoryService
from google.genai.types import Content, Part

from app.memory._writer import write_memory_event
from app.memory.adapter import VertexMemoryAdapter
from app.memory.memory_list import (
    _FETCH_CAP,
    list_memory_writes,
    parse_marker,
)

APP_NAME = "test_app"
USER_ID = "test_user"
OTHER_USER = "other_user"
SESSION_ID = "test_session"


# ---------------------------------------------------------------------------
# parse_marker
# ---------------------------------------------------------------------------


def test_parse_marker_user():
    assert parse_marker("[user] I prefer espresso") == (
        "user",
        "I prefer espresso",
    )


def test_parse_marker_agent():
    assert parse_marker("[agent] retries are flaky in dev") == (
        "agent",
        "retries are flaky in dev",
    )


def test_parse_marker_user_profile_multiline():
    assert parse_marker("[user_profile]\nname: Sam\nrole: SWE") == (
        "user_profile",
        "name: Sam\nrole: SWE",
    )


def test_parse_marker_unmarked_returns_none():
    assert parse_marker("Hey, just chatting") is None
    assert parse_marker("") is None
    assert parse_marker("[other] something else") is None


# ---------------------------------------------------------------------------
# InMemory branch
# ---------------------------------------------------------------------------


async def _seed_user_turn(service: InMemoryMemoryService, text: str) -> None:
    """Mimic auto_capture pushing a plain user turn into Memory Bank.

    Carries no marker prefix and no lha_memory_scope tag — list_memory_writes
    must skip it.
    """
    event = Event(
        invocation_id="auto_capture",
        author="user",
        content=Content(parts=[Part(text=text)]),
    )
    await service.add_events_to_memory(
        app_name=APP_NAME,
        user_id=USER_ID,
        events=[event],
        session_id=SESSION_ID,
    )


@pytest.mark.asyncio
async def test_list_memory_writes_filters_to_marked_events_only():
    service = InMemoryMemoryService()
    # Three intentional writes.
    await write_memory_event(
        text="[user] I prefer espresso",
        scope="user",
        invocation_id="add_memory:user",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    await write_memory_event(
        text="[agent] retries are flaky in dev",
        scope="agent",
        invocation_id="add_memory:agent",
        author="system",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    await write_memory_event(
        text="[user_profile]\nname: Sam",
        scope="user_profile",
        invocation_id="set_user_profile",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    # Noise that must be excluded.
    await _seed_user_turn(service, "Just a regular chat message")

    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=50,
        offset=0,
    )

    scopes = sorted(e.scope for e in result.entries)
    assert scopes == ["agent", "user", "user_profile"]
    assert result.counts == {"user": 1, "agent": 1, "user_profile": 1}
    assert result.has_more is False
    # Marker prefix is stripped from content.
    by_scope = {e.scope: e.content for e in result.entries}
    assert by_scope["user"] == "I prefer espresso"
    assert by_scope["agent"] == "retries are flaky in dev"
    assert by_scope["user_profile"] == "name: Sam"


@pytest.mark.asyncio
async def test_list_memory_writes_orders_newest_first():
    service = InMemoryMemoryService()
    for i in range(3):
        await write_memory_event(
            text=f"[user] fact {i}",
            scope="user",
            invocation_id="add_memory:user",
            author="user",
            memory_service=service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=10,
        offset=0,
    )

    assert [e.content for e in result.entries] == [
        "fact 2",
        "fact 1",
        "fact 0",
    ]


@pytest.mark.asyncio
async def test_list_memory_writes_paginates():
    service = InMemoryMemoryService()
    for i in range(12):
        await write_memory_event(
            text=f"[user] fact {i:02d}",
            scope="user",
            invocation_id="add_memory:user",
            author="user",
            memory_service=service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )

    page1 = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=0,
    )
    page2 = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=5,
    )
    page3 = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=10,
    )

    assert len(page1.entries) == 5
    assert page1.has_more is True
    assert page1.counts == {"user": 12, "agent": 0, "user_profile": 0}

    assert len(page2.entries) == 5
    assert page2.has_more is True

    assert len(page3.entries) == 2
    assert page3.has_more is False


@pytest.mark.asyncio
async def test_list_memory_writes_excludes_other_users():
    service = InMemoryMemoryService()
    await write_memory_event(
        text="[user] mine",
        scope="user",
        invocation_id="add_memory:user",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    await write_memory_event(
        text="[user] not mine",
        scope="user",
        invocation_id="add_memory:user",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=OTHER_USER,
        session_id=SESSION_ID,
    )

    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=10,
        offset=0,
    )

    assert [e.content for e in result.entries] == ["mine"]
    assert result.counts["user"] == 1


@pytest.mark.asyncio
async def test_list_memory_writes_prepends_profile_on_first_page(monkeypatch):
    from app.memory import memory_list as ml

    calls: list[dict] = []

    async def fake_profile(**kw):
        calls.append(kw)
        return "Role: SWE\n- Likes terse answers"

    monkeypatch.setattr(ml, "load_user_profile", fake_profile)
    service = InMemoryMemoryService()
    await write_memory_event(
        text="[user] I prefer espresso",
        scope="user",
        invocation_id="add_memory:user",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=0,
    )
    assert result.entries[0].scope == "user_profile"
    assert result.entries[0].content == "Role: SWE\n- Likes terse answers"
    assert result.counts["user_profile"] == 1
    # The panel shows the whole profile; the prompt path does not.
    assert calls[0]["include_lists"] is True


@pytest.mark.asyncio
async def test_list_memory_writes_omits_profile_on_later_pages(monkeypatch):
    from app.memory import memory_list as ml

    async def fake_profile(**_kw):
        return "Role: SWE"

    monkeypatch.setattr(ml, "load_user_profile", fake_profile)
    service = InMemoryMemoryService()
    for i in range(8):
        await write_memory_event(
            text=f"[user] fact {i}",
            scope="user",
            invocation_id="add_memory:user",
            author="user",
            memory_service=service,
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=SESSION_ID,
        )
    page2 = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=5,
    )
    assert all(e.scope != "user_profile" for e in page2.entries)


@pytest.mark.asyncio
async def test_list_memory_writes_empty():
    service = InMemoryMemoryService()
    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=0,
    )
    assert result.entries == []
    assert result.counts == {"user": 0, "agent": 0, "user_profile": 0}
    assert result.has_more is False


@pytest.mark.asyncio
async def test_list_memory_writes_ties_break_by_insertion_order_newest_first():
    # Hand-build events with identical timestamps so we exercise the sort
    # tiebreak path directly rather than relying on event.timestamp drift.
    service = InMemoryMemoryService()
    same_ts = 1700000000.0
    for i in range(5):
        event = Event(
            invocation_id="add_memory:user",
            author="user",
            content=Content(parts=[Part(text=f"[user] fact {i}")]),
            custom_metadata={"lha_memory_scope": "user"},
        )
        event.timestamp = same_ts
        await service.add_events_to_memory(
            app_name=APP_NAME,
            user_id=USER_ID,
            events=[event],
            session_id=SESSION_ID,
        )

    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=10,
        offset=0,
    )

    assert [e.content for e in result.entries] == [
        "fact 4",
        "fact 3",
        "fact 2",
        "fact 1",
        "fact 0",
    ]


@pytest.mark.asyncio
async def test_list_memory_writes_carries_session_id_in_memory():
    service = InMemoryMemoryService()
    await write_memory_event(
        text="[user] fact",
        scope="user",
        invocation_id="add_memory:user",
        author="user",
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id="session-abc",
    )
    result = await list_memory_writes(
        memory_service=service,
        app_name=APP_NAME,
        user_id=USER_ID,
        limit=5,
        offset=0,
    )
    assert result.entries[0].session_id == "session-abc"


# ---------------------------------------------------------------------------
# Vertex branch — stubbed via duck-typed fake service
# ---------------------------------------------------------------------------


@dataclass
class _FakeMemory:
    fact: str
    update_time: datetime


@dataclass
class _FakeRetrievedMemory:
    memory: _FakeMemory


class _FakePager:
    """Mimics google-genai's AsyncPager: exposes ``.page`` (the first page) and
    refuses async iteration — in the live SDK that path calls ``next_page()``,
    which injects ``page_token`` into the request and is rejected by the
    installed vertexai SDK. ``_walk_vertex`` must read ``.page`` only.
    """

    def __init__(self, items: list[Any]) -> None:
        self.page = list(items)

    def __aiter__(self):
        raise AssertionError(
            "next_page() is broken; _walk_vertex must read .page"
        )


class _FakeAgentEnginesMemories:
    def __init__(self, items: list[_FakeMemory]) -> None:
        self._items = items
        self.last_retrieve_kwargs: dict[str, Any] | None = None

    async def retrieve(self, **kwargs: Any):
        self.last_retrieve_kwargs = kwargs
        return _FakePager([_FakeRetrievedMemory(memory=m) for m in self._items])


class _FakeAgentEngines:
    def __init__(self, memories: _FakeAgentEnginesMemories) -> None:
        self.memories = memories


class _FakeApiClient:
    def __init__(self, engines: _FakeAgentEngines) -> None:
        self.agent_engines = engines


class _FakeVertexMemoryService:
    def __init__(self, items: list[_FakeMemory]) -> None:
        self._agent_engine_id = "fake-engine"
        self._project = "fake-proj"
        self._location = "us-central1"
        self._memories_stub = _FakeAgentEnginesMemories(items)
        self._client = _FakeApiClient(_FakeAgentEngines(self._memories_stub))

    def _get_api_client(self):
        return self._client


@pytest.mark.asyncio
async def test_walk_vertex_filters_and_parses():
    items = [
        _FakeMemory(
            fact="[user] coffee preference",
            update_time=datetime(2026, 5, 1, tzinfo=UTC),
        ),
        _FakeMemory(
            fact="[agent] dev quirk",
            update_time=datetime(2026, 5, 2, tzinfo=UTC),
        ),
        _FakeMemory(
            fact="[user_profile]\nname: Sam",
            update_time=datetime(2026, 5, 3, tzinfo=UTC),
        ),
        # Vertex-distilled fact with no marker — exclude.
        _FakeMemory(
            fact="The user mentioned they like coffee.",
            update_time=datetime(2026, 5, 4, tzinfo=UTC),
        ),
    ]
    service = _FakeVertexMemoryService(items)

    entries = await VertexMemoryAdapter(service).list_all(
        app_name=APP_NAME, user_id=USER_ID
    )

    # Marker-tagged entries keep their scope; the unmarked, Memory-Bank-distilled
    # fact is surfaced as a plain "user" memory rather than dropped.
    scopes = sorted(e.scope for e in entries)
    assert scopes == ["agent", "user", "user", "user_profile"]
    contents = {e.content for e in entries}
    assert "coffee preference" in contents
    assert "dev quirk" in contents
    assert "name: Sam" in contents
    assert "The user mentioned they like coffee." in contents
    # session_id is not preserved by Vertex distillation.
    assert all(e.session_id is None for e in entries)

    # Scope arg was forwarded so server-side filter applies.
    kwargs = service._memories_stub.last_retrieve_kwargs
    assert kwargs is not None
    assert kwargs["scope"] == {"app_name": APP_NAME, "user_id": USER_ID}
    # Fully-qualified resource name, not the bare reasoningEngines/{id} form.
    assert (
        kwargs["name"]
        == "projects/fake-proj/locations/us-central1/reasoningEngines/fake-engine"
    )


@pytest.mark.asyncio
async def test_walk_vertex_reads_first_page_without_paginating():
    # Regression: the installed vertexai SDK rejects page_token in the retrieve
    # request, so _walk_vertex must read the pager's first page and never trigger
    # next_page (the fake pager raises if async-iterated).
    items = [
        _FakeMemory(
            fact="[user] one", update_time=datetime(2026, 5, 1, tzinfo=UTC)
        ),
        _FakeMemory(
            fact="[user] two", update_time=datetime(2026, 5, 2, tzinfo=UTC)
        ),
    ]
    service = _FakeVertexMemoryService(items)

    entries = await VertexMemoryAdapter(service).list_all(
        app_name=APP_NAME, user_id=USER_ID
    )

    assert sorted(e.content for e in entries) == ["one", "two"]
    # One page sized at the fetch cap covers the list — no second-page request.
    kwargs = service._memories_stub.last_retrieve_kwargs
    assert kwargs["simple_retrieval_params"] == {"page_size": _FETCH_CAP}


@pytest.mark.asyncio
async def test_walk_vertex_includes_unmarked_facts_as_user():
    items = [
        _FakeMemory(
            fact="I like Paris.", update_time=datetime(2026, 5, 1, tzinfo=UTC)
        ),
        _FakeMemory(
            fact="   ", update_time=datetime(2026, 5, 2, tzinfo=UTC)
        ),  # blank → skipped
    ]
    service = _FakeVertexMemoryService(items)

    entries = await VertexMemoryAdapter(service).list_all(
        app_name=APP_NAME, user_id=USER_ID
    )

    assert len(entries) == 1
    assert entries[0].scope == "user"
    assert entries[0].content == "I like Paris."
