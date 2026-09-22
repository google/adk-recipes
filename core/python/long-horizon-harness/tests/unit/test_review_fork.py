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

"""Background self-improvement fork — pins the module contract.

(``_run_review_in_thread`` + ``spawn_background_review_thread``) into an
ADK ``after_agent_callback``. The fork runs with a restricted toolset
(memory + skills only), writes land in the parent's ``memory_service``
so the next session's ``PreloadMemoryTool`` surfaces them, and the
parent's session is never mutated by the fork.

Tests use stubbed Runners — the real fork agent is lazily constructed
and never built in unit tests so they don't hit Vertex.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.events.event import Event
from google.adk.memory import InMemoryMemoryService
from google.genai.types import Content, Part

# =============================================================================
# Helpers
# =============================================================================


def _make_event(text: str, *, author: str = "user") -> Event:
    return Event(
        author=author,
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id="inv-test",
    )


def _make_fake_runner(events: list[Event]) -> Any:
    """A Runner stand-in whose ``run_async`` yields the supplied events."""

    async def fake_run_async(**_kwargs: Any) -> AsyncIterator[Event]:
        for event in events:
            yield event

    runner = MagicMock()
    runner.run_async = fake_run_async
    return runner


def _build_callback_context(
    *,
    memory_service: Any,
    state: dict[str, Any] | None = None,
    events: list[Event] | None = None,
    app_name: str = "parent_app",
    user_id: str = "parent_user",
    session_id: str = "parent_session",
) -> SimpleNamespace:
    session = SimpleNamespace(id=session_id, events=list(events or []))
    return SimpleNamespace(
        state=dict(state or {}),
        _invocation_context=SimpleNamespace(
            memory_service=memory_service,
            app_name=app_name,
            user_id=user_id,
            session=session,
        ),
    )


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_entrypoints():
    from horizon.memory.review_fork import (
        REVIEW_FORK_ENABLED_ENV,
        review_fork_callback,
    )

    assert REVIEW_FORK_ENABLED_ENV == "LHA_REVIEW_FORK"
    assert callable(review_fork_callback)


# =============================================================================
# Prompt selection
# =============================================================================


class TestPromptSelection:
    def test_combined_when_skill_activity(self):
        from horizon.memory.review_fork import _pick_prompt
        from horizon.memory.review_prompts import COMBINED_REVIEW_PROMPT

        telem = {
            "git-rebase": {
                "views": 0,
                "manages": 1,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        assert _pick_prompt(telem) is COMBINED_REVIEW_PROMPT

    def test_combined_when_only_views(self):
        from horizon.memory.review_fork import _pick_prompt
        from horizon.memory.review_prompts import COMBINED_REVIEW_PROMPT

        telem = {
            "docs-reader": {
                "views": 3,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        assert _pick_prompt(telem) is COMBINED_REVIEW_PROMPT

    def test_memory_only_when_empty_telemetry(self):
        from horizon.memory.review_fork import _pick_prompt
        from horizon.memory.review_prompts import MEMORY_REVIEW_PROMPT

        assert _pick_prompt({}) is MEMORY_REVIEW_PROMPT

    def test_memory_only_when_none(self):
        from horizon.memory.review_fork import _pick_prompt
        from horizon.memory.review_prompts import MEMORY_REVIEW_PROMPT

        assert _pick_prompt(None) is MEMORY_REVIEW_PROMPT

    def test_memory_only_when_all_zero(self):
        from horizon.memory.review_fork import _pick_prompt
        from horizon.memory.review_prompts import MEMORY_REVIEW_PROMPT

        telem = {
            "stale": {
                "views": 0,
                "manages": 0,
                "last_used": "2026-05-19T10:00:00+00:00",
            }
        }
        assert _pick_prompt(telem) is MEMORY_REVIEW_PROMPT


# =============================================================================
# Snapshot formatting
# =============================================================================


class TestSnapshotFormat:
    def test_wraps_in_conversation_markers(self):
        from horizon.memory.review_fork import _format_snapshot

        events = [_make_event("hello", author="user")]
        out = _format_snapshot(events)
        assert "<CONVERSATION>" in out
        assert "</CONVERSATION>" in out
        assert "hello" in out
        assert "user" in out

    def test_includes_author_labels(self):
        from horizon.memory.review_fork import _format_snapshot

        events = [
            _make_event("question", author="user"),
            _make_event("answer", author="root_agent"),
        ]
        out = _format_snapshot(events)
        assert "user" in out
        assert "root_agent" in out

    def test_empty_events_still_wrapped(self):
        from horizon.memory.review_fork import _format_snapshot

        out = _format_snapshot([])
        assert "<CONVERSATION>" in out
        assert "</CONVERSATION>" in out

    def test_skips_events_without_text(self):
        from horizon.memory.review_fork import _format_snapshot

        bare = Event(author="system", invocation_id="x")
        out = _format_snapshot([bare, _make_event("real")])
        assert "real" in out


# =============================================================================
# Tool whitelist
# =============================================================================


@pytest.mark.asyncio
class TestToolWhitelist:
    async def test_allows_memory(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="memory")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert result is None

    async def test_allows_load_skill(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="load_skill")
        result = await _whitelist_tools_callback(
            tool=tool, args={"skill_name": "x"}, tool_context=SimpleNamespace()
        )
        assert result is None

    async def test_allows_load_skill_with_resource(self):
        """load_skill also covers what used to be a separate
        load_skill_resource tool — same tool name, an extra arg."""
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="load_skill")
        result = await _whitelist_tools_callback(
            tool=tool,
            args={"skill_name": "x", "resource": "references/n.md"},
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_allows_load_skill_reload_action(self):
        """reload folded into load_skill(action='reload') — same tool name,
        no skill_name needed."""
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="load_skill")
        result = await _whitelist_tools_callback(
            tool=tool, args={"action": "reload"}, tool_context=SimpleNamespace()
        )
        assert result is None

    async def test_allows_write_to_skills_path(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="write")
        result = await _whitelist_tools_callback(
            tool=tool,
            args={"path": ".agents/skills/foo/SKILL.md", "content": "..."},
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_allows_edit_on_skills_path(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="edit")
        result = await _whitelist_tools_callback(
            tool=tool,
            args={
                "path": ".agents/skills/foo/SKILL.md",
                "old_string": "a",
                "new_string": "b",
            },
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_denies_write_outside_skills(self):
        """The fork must not be able to scribble over arbitrary workspace
        files — only ``.agents/skills/<name>/...`` is in scope."""
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="write")
        result = await _whitelist_tools_callback(
            tool=tool,
            args={"path": "reports/q3.md", "content": "..."},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert ".agents/skills/" in result["error"]

    async def test_denies_write_at_skills_root(self):
        """``.agents/skills/README.md`` has no ``<name>/`` after the prefix —
        not attributable to a skill, so it's blocked."""
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="write")
        result = await _whitelist_tools_callback(
            tool=tool,
            args={"path": ".agents/skills/README.md", "content": "..."},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert "error" in result

    async def test_denies_terminal(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="terminal")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "terminal" in result["error"]

    async def test_denies_delegate(self):
        from horizon.memory.review_fork import _whitelist_tools_callback

        tool = SimpleNamespace(name="delegate")
        result = await _whitelist_tools_callback(
            tool=tool, args={}, tool_context=SimpleNamespace()
        )
        assert isinstance(result, dict)
        assert "error" in result


# =============================================================================
# Callback wiring — delegates to SiblingAgentPlugin.spawn_sibling
# =============================================================================


@pytest.mark.asyncio
class TestReviewForkCallback:
    async def test_noop_without_memory_service(self, monkeypatch):
        from horizon import agent as agent_mod
        from horizon.memory import review_fork

        spawn_calls: list[dict[str, Any]] = []

        def fake_spawn(**kwargs: Any) -> Any:
            spawn_calls.append(kwargs)
            return MagicMock()

        monkeypatch.setattr(
            agent_mod.SIBLING_AGENT_PLUGIN, "spawn_sibling", fake_spawn
        )

        ctx = _build_callback_context(memory_service=None)
        result = await review_fork.review_fork_callback(ctx)
        assert result is None
        assert spawn_calls == []

    async def test_noop_when_env_disabled(self, monkeypatch):
        from horizon import agent as agent_mod
        from horizon.memory import review_fork

        monkeypatch.setenv("LHA_REVIEW_FORK", "0")

        spawn_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            agent_mod.SIBLING_AGENT_PLUGIN,
            "spawn_sibling",
            lambda **kw: spawn_calls.append(kw) or MagicMock(),
        )

        ctx = _build_callback_context(memory_service=InMemoryMemoryService())
        result = await review_fork.review_fork_callback(ctx)
        assert result is None
        assert spawn_calls == []

    async def test_spawns_sibling_when_enabled(self, monkeypatch):
        from horizon import agent as agent_mod
        from horizon.memory import review_fork

        spawn_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            agent_mod.SIBLING_AGENT_PLUGIN,
            "spawn_sibling",
            lambda **kw: spawn_calls.append(kw) or MagicMock(),
        )
        monkeypatch.setattr(
            review_fork,
            "_get_review_agent",
            lambda: MagicMock(name="fake_agent"),
        )

        parent_memory = InMemoryMemoryService()
        ctx = _build_callback_context(
            memory_service=parent_memory,
            events=[_make_event("hi", author="user")],
        )
        result = await review_fork.review_fork_callback(ctx)

        assert result is None
        assert len(spawn_calls) == 1
        call = spawn_calls[0]
        assert call["app_name"] == "parent_app"
        assert call["user_id"] == "parent_user"
        assert call["memory_service"] is parent_memory
        assert "<CONVERSATION>" in call["prompt"]
        assert call["log_prefix"] == "review_fork"
