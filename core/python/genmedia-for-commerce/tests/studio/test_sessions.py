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

"""ADK sessions in the (fake) media bucket, shared by several instances."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.adk.cli.service_registry import get_service_registry
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.events import Event, EventActions
from google.adk.sessions.base_session_service import GetSessionConfig
from google.genai import types

from genmedia4commerce.core import gcs
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.studio import sessions, store
from genmedia4commerce.studio.session_cache import SessionCache
from genmedia4commerce.studio.sessions import StudioSessionService

APP, USER = "genmedia4commerce", "u"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_BACKOFF_S", 0)


def event(text: str, ts: float, **actions) -> Event:
    return Event(
        invocation_id="i",
        author="user",
        timestamp=ts,
        content=types.Content(role="user", parts=[types.Part(text=text)]),
        actions=EventActions(**actions),
    )


def texts(session) -> list[str]:
    return [e.content.parts[0].text for e in session.events]


async def get(service, sid: str = "s1", user: str = USER, **config):
    return await service.get_session(
        app_name=APP,
        user_id=user,
        session_id=sid,
        config=GetSessionConfig(**config) if config else None,
    )


@pytest.mark.asyncio
async def test_create_get_and_events_in_time_order(storage) -> None:
    service = StudioSessionService()
    session = await service.create_session(
        app_name=APP, user_id=USER, session_id="s1", state={"a": 1}
    )
    assert session.id == "s1" and session.state == {"a": 1}
    # Appended out of time order: read back in time order.
    for text, ts in (("second", 20.0), ("first", 10.0), ("third", 30.0)):
        await service.append_event(session, event(text, ts))
    got = await get(service)
    assert texts(got) == ["first", "second", "third"]
    assert got.state == {"a": 1}
    assert got.last_update_time >= 30.0
    doc = json.loads(
        storage.objects[storage.media("sessions/studio-s1/adk/session.json")]
    )
    assert doc["id"] == "s1" and doc["user_id"] == USER
    with pytest.raises(AlreadyExistsError):
        await service.create_session(
            app_name=APP, user_id=USER, session_id="s1"
        )


@pytest.mark.asyncio
async def test_get_honours_the_config_and_the_owner(storage) -> None:
    service = StudioSessionService()
    session = await service.create_session(
        app_name=APP, user_id=USER, session_id="s1"
    )
    for i in range(5):
        await service.append_event(session, event(f"e{i}", 100.0 + i))
    assert texts(await get(service, num_recent_events=2)) == ["e3", "e4"]
    assert texts(await get(service, num_recent_events=0)) == []
    # More than there are: all of them.
    assert len((await get(service, num_recent_events=7)).events) == 5
    assert texts(await get(service, after_timestamp=102.0)) == [
        "e2",
        "e3",
        "e4",
    ]
    assert await get(service, user="someone else") is None
    assert await get(service, sid="missing") is None


@pytest.mark.asyncio
async def test_state_deltas_are_saved_but_not_temp_or_partial_events(
    storage,
) -> None:
    service = StudioSessionService()
    session = await service.create_session(
        app_name=APP, user_id=USER, session_id="s1"
    )
    await service.append_event(
        session, event("a", 1.0, state_delta={"k": 1, "temp:t": 2})
    )
    partial = event("p", 2.0)
    partial.partial = True
    await service.append_event(session, partial)
    assert session.state == {"k": 1, "temp:t": 2}  # this invocation
    got = await get(StudioSessionService())
    assert got.state == {"k": 1}
    assert texts(got) == ["a"]


@pytest.mark.asyncio
async def test_two_instances_share_a_session(storage) -> None:
    one, two = StudioSessionService(), StudioSessionService()
    created = await one.create_session(
        app_name=APP, user_id=USER, session_id="s1"
    )
    await one.append_event(created, event("from one", 1.0))
    on_two = await get(two)
    await two.append_event(on_two, event("from two", 2.0, state_delta={"x": 1}))
    back = await get(one)
    assert texts(back) == ["from one", "from two"]
    assert back.state == {"x": 1}


@pytest.mark.asyncio
async def test_concurrent_appends_and_state_changes_are_all_kept(
    storage,
) -> None:
    services = [StudioSessionService() for _ in range(4)]
    await services[0].create_session(
        app_name=APP, user_id=USER, session_id="s1"
    )
    copies = [await get(s) for s in services]
    await asyncio.gather(
        *(
            s.append_event(c, event(f"e{i}", 1.0, state_delta={f"k{i}": i}))
            for i, (s, c) in enumerate(zip(services, copies, strict=True))
        )
    )
    got = await get(services[0])
    assert sorted(texts(got)) == ["e0", "e1", "e2", "e3"]
    assert got.state == {"k0": 0, "k1": 1, "k2": 2, "k3": 3}


@pytest.mark.asyncio
async def test_list_and_delete(storage) -> None:
    service = StudioSessionService()
    for sid, user in (("a", USER), ("b", USER), ("c", "other")):
        s = await service.create_session(
            app_name=APP, user_id=user, session_id=sid
        )
        await service.append_event(s, event("x", 1.0))
    storage.objects[storage.media("sessions/studio-jobs/j1.json")] = b"{}"
    listed = await service.list_sessions(app_name=APP, user_id=USER)
    assert sorted(s.id for s in listed.sessions) == ["a", "b"]
    everyone = await service.list_sessions(app_name=APP)
    assert len(everyone.sessions) == 3
    assert not (await service.list_sessions(app_name="other")).sessions
    await service.delete_session(app_name=APP, user_id="other", session_id="a")
    assert await get(service, "a") is not None  # not the owner: kept
    await service.delete_session(app_name=APP, user_id=USER, session_id="a")
    assert await get(service, "a") is None
    assert not any("studio-a/" in uri for uri in storage.objects)
    assert await get(service, "b") is not None


def test_gcs_delete_is_only_for_session_documents(storage) -> None:
    for uri in (
        storage.media("runs/d/s/r/outputs/a.png"),
        storage.media("sessions/../runs/a.png"),
        "gs://other-bucket/sessions/a.json",
    ):
        with pytest.raises(InputError):
            gcs.delete(uri)


@pytest.mark.asyncio
async def test_add_event_appends_to_an_existing_session_of_any_user(
    storage,
) -> None:
    service = StudioSessionService()
    await service.create_session(app_name=APP, user_id=USER, session_id="w1")
    assert sessions.add_event("w1", event("[studio] a fact", 5.0))
    assert texts(await get(service, "w1")) == ["[studio] a fact"]


def _calls(storage, monkeypatch, method: str) -> list[str]:
    """The URIs ``storage.<method>`` is called with from now on."""
    seen: list[str] = []
    real = getattr(storage, method)

    def counted(uri: str, *args, **kwargs):
        seen.append(uri)
        return real(uri, *args, **kwargs)

    monkeypatch.setattr(storage, method, counted)
    return seen


def _events_in(uris: list[str]) -> list[str]:
    return [uri for uri in uris if "/adk/events/" in uri]


@pytest.mark.asyncio
async def test_a_process_reads_each_event_object_once(
    storage, monkeypatch
) -> None:
    service = StudioSessionService()
    session = await service.create_session(
        app_name=APP, user_id=USER, session_id="s1"
    )
    for i in range(3):
        await service.append_event(session, event(f"e{i}", 1.0 + i))
    # A new process: it has read none of them.
    monkeypatch.setattr(sessions, "_CACHE", SessionCache())
    reads = _calls(storage, monkeypatch, "download")
    got = await get(service)
    assert texts(got) == ["e0", "e1", "e2"]
    assert len(_events_in(reads)) == 3
    # Another instance appends one: only that object is read.
    mine = sessions._CACHE
    monkeypatch.setattr(sessions, "_CACHE", SessionCache())
    await StudioSessionService().append_event(session, event("e3", 4.0))
    monkeypatch.setattr(sessions, "_CACHE", mine)
    got.events[0].content.parts[0].text = "changed by a caller"
    reads.clear()
    assert texts(await get(service)) == ["e0", "e1", "e2", "e3"]
    (read,) = _events_in(reads)
    assert read.endswith(f"-{session.events[-1].id}.json")
    # What this process writes is never read back.
    assert sessions.add_event("s1", event("a note", 5.0))
    reads.clear()
    assert texts(await get(service))[-1] == "a note"
    assert not _events_in(reads)


def test_a_process_checks_once_that_a_session_exists(
    storage, monkeypatch
) -> None:
    writes = _calls(storage, monkeypatch, "write")
    for i in range(3):
        assert sessions.add_event("n1", event(f"note {i}", 1.0 + i))
    meta = storage.media("sessions/studio-n1/adk/session.json")
    assert writes.count(meta) == 1 and len(_events_in(writes)) == 3


def test_the_studio_scheme_is_registered() -> None:
    sessions.register()
    service = get_service_registry().create_session_service(
        "studio://", agents_dir="."
    )
    assert isinstance(service, StudioSessionService)
