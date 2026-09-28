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

"""User profile backed by Memory Bank native Structured Profiles.

``load_user_profile`` reads the profile via ``retrieve_profiles`` and
renders the structured fields into a system-prompt block;
``render_user_profile(state)`` formats ``state['user_profile']`` under a
``## User Profile`` heading. Profile *writes* now happen server-side via
``memories.generate`` (see ``test_dream_review.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.memory import InMemoryMemoryService

from app.infrastructure.memory_config import USER_PROFILE_SCHEMA_ID
from app.memory.adapter import VertexMemoryAdapter
from app.memory.add_memory_tool import add_memory_entry
from app.memory.user_profile import (
    _render_profile,
    engine_resource_name,
    load_user_profile,
    render_user_profile,
)

APP_NAME = "test_app"
USER_ID = "test_user"
SESSION_ID = "test_session"


def _profile_client(profiles: dict[str, Any]) -> Any:
    """Fake Vertex client whose retrieve_profiles returns ``profiles``."""

    async def retrieve_profiles(*, name, scope, config=None):
        del name, scope, config
        return SimpleNamespace(profiles=profiles)

    return SimpleNamespace(
        agent_engines=SimpleNamespace(
            memories=SimpleNamespace(retrieve_profiles=retrieve_profiles)
        )
    )


def _vertex_adapter(client: Any) -> VertexMemoryAdapter:
    """A VertexMemoryAdapter over a fake service exposing ``client``."""
    service = SimpleNamespace(
        _get_api_client=lambda: client,
        _agent_engine_id="e",
        _project=None,
        _location=None,
    )
    return VertexMemoryAdapter(service)


# =========================================================================
# engine_resource_name
# =========================================================================


class TestEngineResourceName:
    def test_builds_full_name_from_project_and_location(self, monkeypatch):
        monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
        svc = SimpleNamespace(
            _agent_engine_id="456", _project="my-proj", _location="us-central1"
        )
        assert (
            engine_resource_name(svc)
            == "projects/my-proj/locations/us-central1/reasoningEngines/456"
        )

    def test_prefers_pinned_env_for_same_engine(self, monkeypatch):
        monkeypatch.setenv(
            "AGENT_ENGINE_RESOURCE_NAME",
            "projects/pinned/locations/europe-west1/reasoningEngines/456",
        )
        svc = SimpleNamespace(
            _agent_engine_id="456", _project="my-proj", _location="us-central1"
        )
        assert (
            engine_resource_name(svc)
            == "projects/pinned/locations/europe-west1/reasoningEngines/456"
        )

    def test_ignores_pinned_env_for_different_engine(self, monkeypatch):
        monkeypatch.setenv(
            "AGENT_ENGINE_RESOURCE_NAME",
            "projects/pinned/locations/europe-west1/reasoningEngines/999",
        )
        svc = SimpleNamespace(
            _agent_engine_id="456", _project="my-proj", _location="us-central1"
        )
        assert (
            engine_resource_name(svc)
            == "projects/my-proj/locations/us-central1/reasoningEngines/456"
        )

    def test_falls_back_to_relative_without_project_or_location(
        self, monkeypatch
    ):
        monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
        svc = SimpleNamespace(
            _agent_engine_id="456", _project=None, _location=None
        )
        assert engine_resource_name(svc) == "reasoningEngines/456"


# =========================================================================
# _render_profile
# =========================================================================


class TestRenderProfileDict:
    def test_renders_summary_and_fields(self):
        out = _render_profile(
            {
                "summary": "Alice is an SRE.",
                "role": "SRE",
                "interests": ["observability", "k8s"],
                "working_style": "terse",
                "durable_facts": ["PST timezone"],
            },
            include_lists=True,
        )
        assert out.startswith("Alice is an SRE.")
        assert "**Role:** SRE" in out
        assert "**Interests:** observability, k8s" in out
        assert "**Working style:** terse" in out
        assert "- PST timezone" in out
        assert out.index("Role") < out.index("Interests") < out.index("style")

    def test_skips_empty_fields(self):
        out = _render_profile(
            {"summary": "Just a summary.", "interests": []},
            include_lists=True,
        )
        assert out == "Just a summary."

    def test_skips_blank_list_items(self):
        out = _render_profile(
            {"summary": "S", "durable_facts": ["", "   ", "real"]},
            include_lists=True,
        )
        assert out == "S\n\n- real"

    def test_empty_profile_renders_empty(self):
        assert _render_profile({}) == ""

    def test_omits_lists_by_default(self):
        out = _render_profile(
            {
                "summary": "Alice is an SRE.",
                "role": "SRE",
                "working_style": "terse",
                "interests": ["observability", "k8s"],
                "durable_facts": ["PST timezone"],
            }
        )
        assert out.startswith("Alice is an SRE.")
        assert "**Role:** SRE" in out
        assert "**Working style:** terse" in out
        assert "Interests" not in out
        assert "PST timezone" not in out

    def test_falls_back_to_lists_when_no_narrative_fields(self):
        out = _render_profile(
            {"interests": ["k8s"], "durable_facts": ["PST timezone"]}
        )
        assert "**Interests:** k8s" in out
        assert "- PST timezone" in out

    def test_any_narrative_field_suppresses_the_fallback(self):
        out = _render_profile({"role": "SRE", "durable_facts": ["PST"]})
        assert out == "- **Role:** SRE"

    def test_blank_narrative_fields_still_fall_back(self):
        out = _render_profile({"summary": "  ", "durable_facts": ["x"]})
        assert out == "- x"

    def test_scalar_string_list_field_is_not_split_into_characters(self):
        out = _render_profile(
            {"interests": "Python", "durable_facts": "Likes tea"},
            include_lists=True,
        )
        assert "**Interests:** Python" in out
        assert "- Likes tea" in out
        assert "P, y, t" not in out

    def test_deduplicates_list_items_case_insensitively(self):
        out = _render_profile(
            {
                "interests": ["Multi-agent systems", "multi-agent systems"],
                "durable_facts": ["Dev ergonomics", "dev ergonomics"],
            },
            include_lists=True,
        )
        assert "**Interests:** Multi-agent systems" in out
        assert out.count("agent systems") == 1
        assert out.count("ev ergonomics") == 1

    def test_strips_existing_bullet_markers_from_facts(self):
        out = _render_profile(
            {"durable_facts": ["- Fact A", "* Fact B", "- - Fact C"]},
            include_lists=True,
        )
        assert out == "- Fact A\n- Fact B\n- Fact C"

    def test_drops_non_string_list_items(self):
        out = _render_profile(
            {"summary": "S", "durable_facts": ["real", None, 5, ["x"]]},
            include_lists=True,
        )
        assert out == "S\n\n- real"

    def test_ignores_non_sequence_list_fields(self):
        out = _render_profile(
            {"summary": "Just a summary.", "interests": 5, "durable_facts": 7},
            include_lists=True,
        )
        assert out == "Just a summary."


# =========================================================================
# load_user_profile — retrieve_profiles path
# =========================================================================


@pytest.mark.asyncio
class TestLoadUserProfile:
    async def test_non_vertex_service_returns_empty(self):
        profile = await load_user_profile(
            memory_service=InMemoryMemoryService(),
            app_name=APP_NAME,
            user_id=USER_ID,
        )
        assert profile == ""

    async def test_omits_lists_for_the_prompt_path(self, monkeypatch):
        from app.memory import user_profile as up

        client = _profile_client(
            {
                USER_PROFILE_SCHEMA_ID: SimpleNamespace(
                    profile={
                        "summary": "Hi Alice.",
                        "durable_facts": ["PST timezone"],
                    }
                )
            }
        )
        monkeypatch.setattr(
            up, "memory_adapter", lambda _ms: _vertex_adapter(client)
        )
        assert (
            await load_user_profile(
                memory_service=object(), app_name=APP_NAME, user_id=USER_ID
            )
            == "Hi Alice."
        )
        full = await load_user_profile(
            memory_service=object(),
            app_name=APP_NAME,
            user_id=USER_ID,
            include_lists=True,
        )
        assert "PST timezone" in full

    async def test_returns_rendered_profile(self, monkeypatch):
        from app.memory import user_profile as up

        client = _profile_client(
            {
                USER_PROFILE_SCHEMA_ID: SimpleNamespace(
                    profile={"summary": "Hi Alice."}
                )
            }
        )
        monkeypatch.setattr(
            up, "memory_adapter", lambda _ms: _vertex_adapter(client)
        )
        profile = await load_user_profile(
            memory_service=object(), app_name=APP_NAME, user_id=USER_ID
        )
        assert profile == "Hi Alice."

    async def test_missing_schema_id_returns_empty(self, monkeypatch):
        from app.memory import user_profile as up

        client = _profile_client(
            {"other-schema": SimpleNamespace(profile={"x": 1})}
        )
        monkeypatch.setattr(
            up, "memory_adapter", lambda _ms: _vertex_adapter(client)
        )
        profile = await load_user_profile(
            memory_service=object(), app_name=APP_NAME, user_id=USER_ID
        )
        assert profile == ""

    async def test_empty_profile_returns_empty(self, monkeypatch):
        from app.memory import user_profile as up

        client = _profile_client(
            {USER_PROFILE_SCHEMA_ID: SimpleNamespace(profile={})}
        )
        monkeypatch.setattr(
            up, "memory_adapter", lambda _ms: _vertex_adapter(client)
        )
        profile = await load_user_profile(
            memory_service=object(), app_name=APP_NAME, user_id=USER_ID
        )
        assert profile == ""

    async def test_retrieve_raising_returns_empty(self, monkeypatch):
        from app.memory import user_profile as up

        async def boom(**_kw):
            raise RuntimeError("vertex down")

        client = SimpleNamespace(
            agent_engines=SimpleNamespace(
                memories=SimpleNamespace(retrieve_profiles=boom)
            )
        )
        monkeypatch.setattr(
            up, "memory_adapter", lambda _ms: _vertex_adapter(client)
        )
        profile = await load_user_profile(
            memory_service=object(), app_name=APP_NAME, user_id=USER_ID
        )
        assert profile == ""


# =========================================================================
# render_user_profile(state) — system-prompt-tier renderer
# =========================================================================


class TestRenderUserProfile:
    def test_renders_under_user_profile_heading(self):
        block = render_user_profile(
            {"user_profile": "Name: Alice. Timezone: EST."}
        )
        assert "## User Profile" in block
        assert "Name: Alice" in block
        assert "Timezone: EST" in block

    def test_returns_empty_when_no_profile_in_state(self):
        assert render_user_profile({}) == ""

    def test_returns_empty_when_profile_blank(self):
        assert render_user_profile({"user_profile": ""}) == ""
        assert render_user_profile({"user_profile": "   "}) == ""

    def test_heading_precedes_content(self):
        block = render_user_profile({"user_profile": "Name: Alice."})
        assert block.index("## User Profile") < block.index("Name: Alice")


# =========================================================================
# Regression: add_memory_entry scope='user'/'agent' still APPEND
# =========================================================================


async def _all_entries(service: InMemoryMemoryService) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for query in ("agent", "user", "fact", "note", "x"):
        response = await service.search_memory(
            app_name=APP_NAME, user_id=USER_ID, query=query
        )
        for memory in response.memories:
            for part in (
                memory.content.parts if memory.content else None
            ) or []:
                if part.text and part.text not in seen:
                    seen.add(part.text)
                    out.append(part.text)
    return out


@pytest.mark.asyncio
class TestAppendScopesRegression:
    async def test_user_scope_still_appends(
        self, fake_memory_service: InMemoryMemoryService
    ):
        for content in ("fact one", "fact two"):
            await add_memory_entry(
                content=content,
                scope="user",
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=SESSION_ID,
            )
        entries = await _all_entries(fake_memory_service)
        assert any("fact one" in e for e in entries)
        assert any("fact two" in e for e in entries)

    async def test_agent_scope_still_appends(
        self, fake_memory_service: InMemoryMemoryService
    ):
        for content in ("note alpha", "note beta"):
            await add_memory_entry(
                content=content,
                scope="agent",
                memory_service=fake_memory_service,
                app_name=APP_NAME,
                user_id=USER_ID,
                session_id=SESSION_ID,
            )
        entries = await _all_entries(fake_memory_service)
        assert any("note alpha" in e for e in entries)
        assert any("note beta" in e for e in entries)
