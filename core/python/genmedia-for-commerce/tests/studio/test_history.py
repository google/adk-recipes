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

"""``[studio]`` notes in the agent's session, and ``store.update``."""

from __future__ import annotations

import json

import pytest
from google.api_core.exceptions import TooManyRequests

from genmedia4commerce.studio import history, sessions, store


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_BACKOFF_S", 0)


def _events(storage, folder: str = "studio-s1") -> list[dict]:
    prefix = storage.media(f"sessions/{folder}/adk/events/")
    return [
        json.loads(data)
        for uri, data in sorted(storage.objects.items())
        if uri.startswith(prefix)
    ]


@pytest.mark.asyncio
async def test_a_note_creates_the_session_and_the_next_get_sees_it(
    storage,
) -> None:
    assert history.note("s1", "User uploaded a.png")
    assert history.note("s1", "User picked Emma")
    assert not history.note("s1", "")
    assert not history.note("", "x")
    session = await sessions.StudioSessionService().get_session(
        app_name=sessions.APP_NAME, user_id=sessions.PANEL_USER, session_id="s1"
    )
    assert session is not None
    texts = [e.content.parts[0].text for e in session.events]
    assert texts == [
        "[studio] User uploaded a.png",
        "[studio] User picked Emma",
    ]
    assert {e.author for e in session.events} == {"user"}


def test_a_note_never_reads_the_session(storage, monkeypatch) -> None:
    history.note("s1", "first")

    def no_reads(uri: str) -> bytes:
        raise AssertionError(f"read {uri}")

    monkeypatch.setattr(storage, "download", no_reads)
    assert history.note("s1", "second")
    assert len(_events(storage)) == 2


@pytest.mark.parametrize("studio_id", ["a/b c", "a.b", "jobs", "x" * 58])
def test_a_bad_studio_id_is_never_rewritten(storage, studio_id) -> None:
    """``a.b`` and ``a-b`` used to share a folder; now nothing is noted."""
    assert not history.note(studio_id, "x")
    assert storage.objects == {}


def test_a_failed_note_is_logged_not_raised(storage, monkeypatch, caplog):
    def down(uri, data, if_generation_match=None):
        raise RuntimeError("503")

    monkeypatch.setattr(storage, "write", down)
    assert history.note("s1", "lost") is False
    assert "503" in caplog.text


def test_update_retries_rate_limits_then_gives_up(
    storage, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    real = storage.write
    limited: list[str] = []

    def limit(uri, data, if_generation_match=None):
        if len(limited) < 2:
            limited.append(uri)
            raise TooManyRequests("slow down")
        real(uri, data, if_generation_match)

    monkeypatch.setattr(storage, "write", limit)
    assert store.update("sessions/x.json", lambda doc: {"n": 1})
    assert len(limited) == 2

    def always(uri, data, if_generation_match=None):
        raise TooManyRequests("slow down")

    monkeypatch.setattr(storage, "write", always)
    assert store.update("sessions/x.json", lambda doc: {"n": 2}) is False
    assert "gave up" in caplog.text
    assert store.read("sessions/x.json") == {"n": 1}
