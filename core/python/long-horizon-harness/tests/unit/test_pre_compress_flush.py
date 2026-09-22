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

"""Pre-compression memory flush — pins the module contract.

The flush fork mirrors ``review_fork``'s structure but with a narrower
toolset (``memory`` only) and a different trigger (right before
``HorizonSummarizer.maybe_summarize_events`` calls ``spawn_flush_fork`` before
delegating to ADK's compaction).

Sibling-agent runner construction lives in ``SiblingAgentPlugin`` (see
:mod:`tests.unit.test_sibling_agent_plugin`); these tests focus on
``spawn_flush_fork``'s gating + the plugin hand-off.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.events.event import Event
from google.adk.memory import InMemoryMemoryService
from google.genai.types import Content, Part

pytestmark = pytest.mark.asyncio


def _make_event(text: str, *, author: str = "user") -> Event:
    return Event(
        author=author,
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id="inv-test",
    )


@pytest.fixture
def captured_spawn(monkeypatch):
    """Stub ``SIBLING_AGENT_PLUGIN.spawn_sibling`` and record kwargs."""
    from horizon import agent as agent_mod

    calls: list[dict[str, Any]] = []

    def fake_spawn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(
        agent_mod.SIBLING_AGENT_PLUGIN, "spawn_sibling", fake_spawn
    )
    return calls


# =============================================================================
# Module surface
# =============================================================================


async def test_module_exposes_entrypoints():
    from horizon.memory.flush_fork import (
        FLUSH_FORK_ENABLED_ENV,
        spawn_flush_fork,
    )

    assert FLUSH_FORK_ENABLED_ENV == "LHA_PRE_COMPRESS_FLUSH"
    assert callable(spawn_flush_fork)


# =============================================================================
# Whitelist
# =============================================================================


class TestToolWhitelist:
    async def test_allows_memory(self):
        from horizon.memory.flush_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="memory")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert result is None

    async def test_denies_skill(self):
        # The consolidated ``skill`` dispatch must not run during a
        # pre-compress flush — the flush fork only persists memory; any
        # skill mutation would muddy the curation pass.
        from horizon.memory.flush_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="skill")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert isinstance(result, dict)
        assert "error" in result

    async def test_denies_terminal(self):
        from horizon.memory.flush_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="terminal")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert isinstance(result, dict)
        assert "error" in result


# =============================================================================
# Snapshot formatting
# =============================================================================


class TestSnapshotFormat:
    async def test_wraps_in_conversation_markers(self):
        from horizon.memory.flush_fork import _format_snapshot

        out = _format_snapshot([_make_event("hello", author="user")])
        assert "<CONVERSATION>" in out
        assert "</CONVERSATION>" in out
        assert "hello" in out

    async def test_empty_events_still_wrapped(self):
        from horizon.memory.flush_fork import _format_snapshot

        out = _format_snapshot([])
        assert "<CONVERSATION>" in out
        assert "</CONVERSATION>" in out


# =============================================================================
# spawn_flush_fork — gating behavior
# =============================================================================


class TestSpawnGating:
    async def test_noop_when_env_disabled(self, monkeypatch, captured_spawn):
        from horizon.memory import flush_fork

        monkeypatch.setenv("LHA_PRE_COMPRESS_FLUSH", "0")
        ok = flush_fork.spawn_flush_fork(
            parent_memory_service=InMemoryMemoryService(),
            parent_app_name="a",
            parent_user_id="u",
            events=[_make_event("hi")],
        )
        assert ok is False
        assert captured_spawn == []

    async def test_noop_without_memory_service(self, captured_spawn):
        from horizon.memory import flush_fork

        ok = flush_fork.spawn_flush_fork(
            parent_memory_service=None,
            parent_app_name="a",
            parent_user_id="u",
            events=[_make_event("hi")],
        )
        assert ok is False
        assert captured_spawn == []

    async def test_noop_with_no_events(self, captured_spawn):
        from horizon.memory import flush_fork

        ok = flush_fork.spawn_flush_fork(
            parent_memory_service=InMemoryMemoryService(),
            parent_app_name="a",
            parent_user_id="u",
            events=[],
        )
        assert ok is False
        assert captured_spawn == []

    async def test_spawns_sibling_when_enabled(self, captured_spawn):
        from horizon.memory import flush_fork

        parent_memory = InMemoryMemoryService()
        ok = flush_fork.spawn_flush_fork(
            parent_memory_service=parent_memory,
            parent_app_name="a",
            parent_user_id="u",
            events=[_make_event("hi")],
        )
        assert ok is True
        assert len(captured_spawn) == 1
        call = captured_spawn[0]
        assert call["app_name"] == "a"
        assert call["user_id"] == "u"
        assert call["memory_service"] is parent_memory
        assert "<CONVERSATION>" in call["prompt"]
        # Prompt body should mention compression to set context.
        assert "compress" in call["prompt"].lower()
        assert call["log_prefix"] == "flush_fork"

    async def test_uses_explicit_plugin_handle_when_passed(self):
        from horizon.memory import flush_fork

        explicit_plugin = MagicMock()
        ok = flush_fork.spawn_flush_fork(
            parent_memory_service=InMemoryMemoryService(),
            parent_app_name="a",
            parent_user_id="u",
            events=[_make_event("hi")],
            sibling_agent_plugin=explicit_plugin,
        )
        assert ok is True
        explicit_plugin.spawn_sibling.assert_called_once()
