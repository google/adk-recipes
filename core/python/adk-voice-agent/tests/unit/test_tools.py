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

"""Unit tests for the live agent wiring, grounding tools, Vertex AI default, and latency logging."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from google.adk.utils.env_utils import is_enterprise_mode_enabled
from google.genai import types

from app.agent import root_agent
from app.app_utils import timing
from app.app_utils.markdown import plain_text
from app.app_utils.timing import log_latency
from app.auth import env_store
from app.context_cards import active_context_cards
from app.tools import grounding_tools
from app.tools.grounding_tools import (
    GROUNDING_MODEL,
    Grounded,
    _source,
    search_maps_grounded,
    search_web_grounded,
)


def test_slow_tools_ack_silently():
    """Slow tools send a SILENT pending ack, and connected-app/grounding tools live only on MCP."""
    tool_map = {
        getattr(t, "name", getattr(t, "__name__", "")): t
        for t in (root_agent.tools or [])
    }
    for name in (
        "start_thread",
        "message_thread",
        "approve",
        "cancel_thread",
        "publish_thread",
    ):
        assert (
            getattr(tool_map[name], "response_scheduling", None)
            == types.FunctionResponseScheduling.SILENT
        )
    for delegated in (
        "search_web_grounded",
        "search_maps_grounded",
        "calendar_events",
        "gmail_messages",
        "drive_files",
        "github_operations",
        "spotify_playback",
    ):
        assert delegated not in tool_map


@pytest.mark.asyncio
async def test_grounding_tools(monkeypatch: pytest.MonkeyPatch):
    assert GROUNDING_MODEL == "gemini-3.5-flash-lite"

    async def _fake_specialist(_agent, prompt: str) -> Grounded:
        return Grounded(
            f"Grounded answer for: {prompt}.",
            [
                {
                    "title": "ADK docs",
                    "url": "https://a.dev/1",
                    "domain": "a.dev",
                },
                {
                    "title": "ADK guide",
                    "url": "https://a.dev/2",
                    "domain": "a.dev",
                },
                {"title": "Bookworm", "url": "https://maps.google.com/?cid=7"},
            ],
        )

    monkeypatch.setattr(
        "app.tools.grounding_tools._run_specialist", _fake_specialist
    )
    assert (
        "Grounded answer for: ADK documentation"
        in await search_web_grounded("ADK documentation")
    )
    search = _card_context()
    assert [s["url"] for s in search["items"]] == [
        "https://a.dev/1",
        "https://maps.google.com/?cid=7",
    ]
    assert search["meta"]["search_url"].endswith("q=ADK+documentation")

    assert (
        "Grounded answer for: library near Mountain View"
        in await search_maps_grounded("library", "Mountain View")
    )
    place = _card_context()["items"][-1]
    assert place == {
        "name": "Bookworm",
        "url": "https://maps.google.com/?cid=7",
        "summary": "",
    }

    async def _failing_specialist(_agent, _prompt: str) -> Grounded:
        raise RuntimeError("Upstream grounding quota exceeded")

    monkeypatch.setattr(
        "app.tools.grounding_tools._run_specialist", _failing_specialist
    )
    web_err = await search_web_grounded("latest python release")
    assert "could not complete the request" in web_err
    assert "Upstream grounding quota" not in web_err
    assert "RuntimeError" not in web_err
    maps_err = await search_maps_grounded("coffee shops", "Austin")
    assert "could not complete the request" in maps_err
    assert "Upstream grounding quota" not in maps_err
    assert "RuntimeError" not in maps_err


def _card_context() -> dict:
    (card,) = active_context_cards()
    return card["dataModel"]["context"]


def test_grounding_sources_come_from_web_and_maps_chunks():
    web = types.GroundingChunk(
        web=types.GroundingChunkWeb(uri="https://x.dev/a", title="x.dev")
    )
    place = types.GroundingChunk(
        maps=types.GroundingChunkMaps(
            uri="https://maps/1", title="Cafe", text="Cozy"
        )
    )
    assert _source(web) == {
        "title": "x.dev",
        "url": "https://x.dev/a",
        "domain": "x.dev",
    }
    assert _source(place) == {
        "title": "Cafe",
        "url": "https://maps/1",
        "summary": "Cozy",
    }
    assert _source(types.GroundingChunk()) is None


MARKDOWN_ANSWER = (
    "Here are a few ramen spots:\n"
    "* **Ramen DANBO** at [1222 E Pine St](https://maps.google.com/?cid=1) "
    "is open until 11 PM [1, 2].\n"
    "2. *Menya Muso* (https://menya.example) has a 4.3-star rating [1.33]."
)
PLAIN_ANSWER = (
    "Here are a few ramen spots: Ramen DANBO at 1222 E Pine St is open until "
    "11 PM. Menya Muso has a 4.3-star rating."
)


def test_plain_text_strips_bullets_emphasis_and_links_but_keeps_identifiers():
    assert plain_text(MARKDOWN_ANSWER) == PLAIN_ANSWER
    assert plain_text("Set _DATABASE_URL_ in `test_retry.py`.") == (
        "Set DATABASE_URL in test_retry.py."
    )


@pytest.mark.asyncio
async def test_specialist_answers_reach_voice_and_card_without_markdown(
    monkeypatch: pytest.MonkeyPatch,
):
    class _Runner:
        async def run_async(self, **_):
            yield SimpleNamespace(
                content=types.Content(
                    role="model", parts=[types.Part(text=MARKDOWN_ANSWER)]
                ),
                grounding_metadata=None,
            )

    agent = grounding_tools._maps_specialist
    monkeypatch.setitem(grounding_tools._runners, agent.name, _Runner())
    answer, _ = await grounding_tools._run_specialist(agent, "ramen")
    assert answer == PLAIN_ANSWER


# ---- Vertex AI (Enterprise) default -----------------------------------------


@pytest.fixture
def default_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """`load_dotenv_defaults()` reading `tmp_path/.env` as the default `.env`.

    The whole process environment is restored afterwards, since `.env.example`
    fills every key it has a default for.
    """
    path = tmp_path / ".env"
    monkeypatch.setattr(env_store, "DEFAULT_DOTENV_PATH", path)
    with mock.patch.dict(os.environ):
        for key in (
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "VOICE_AGENT_DOTENV_PATH",
            "GOOGLE_CLOUD_PROJECT",
            "APP_URL",
            "LIVE_VOICE_NAME",
        ):
            os.environ.pop(key, None)
        os.environ["VOICE_AGENT_LOAD_DOTENV"] = "1"
        yield path


def test_vertex_ai_is_the_default_when_dotenv_omits_it(
    default_dotenv: Path,
) -> None:
    default_dotenv.write_text("LIVE_VOICE_NAME=Puck\n")

    env_store.load_dotenv_defaults()

    assert os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] == "true"
    assert is_enterprise_mode_enabled()
    assert os.environ["LIVE_VOICE_NAME"] == "Puck"


def test_an_explicit_value_is_kept(default_dotenv: Path) -> None:
    default_dotenv.write_text("GOOGLE_GENAI_USE_ENTERPRISE=false\n")

    env_store.load_dotenv_defaults()

    assert os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] == "false"
    assert not is_enterprise_mode_enabled()


def test_blank_and_placeholder_defaults_stay_unset(
    default_dotenv: Path,
) -> None:
    default_dotenv.write_text("")

    env_store.load_dotenv_defaults()

    assert "GOOGLE_CLOUD_PROJECT" not in os.environ
    assert "APP_URL" not in os.environ


# ---- latency logging --------------------------------------------------------


def test_log_latency_logs_the_phase_fields_and_elapsed_seconds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    clock = iter([10.0, 13.8125])
    monkeypatch.setattr(timing.time, "perf_counter", lambda: next(clock))

    with caplog.at_level("INFO", logger="app.latency"):
        with log_latency("specialist", model="m") as fields:
            fields["sources"] = 3

    (record,) = caplog.records
    assert record.levelname == "INFO"
    assert (
        record.getMessage()
        == "latency phase=specialist model=m sources=3 elapsed_s=3.812"
    )
    assert record.latency == {  # type: ignore[attr-defined]
        "phase": "specialist",
        "model": "m",
        "sources": 3,
        "elapsed_s": 3.812,
    }


def test_log_latency_still_logs_when_the_block_fails(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level("INFO", logger="app.latency"):
        with pytest.raises(RuntimeError), log_latency("tool", tool="lookup"):
            raise RuntimeError("boom")

    (record,) = caplog.records
    assert record.latency["ok"] is False  # type: ignore[attr-defined]
    assert "phase=tool tool=lookup elapsed_s=" in record.getMessage()
