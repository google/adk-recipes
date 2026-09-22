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

"""Skill telemetry callback tests.

Pins the contract for ``horizon.memory.skill_telemetry.skill_telemetry_callback``:
an ``after_tool_callback`` that increments per-skill counters in
``session.state['skill_telemetry']``.

Shape (per skill name):
    {"views": int, "manages": int, "last_used": "<ISO timestamp>"}

The callback dispatches on the tool name:

* ``load_skill`` (a skill's instructions, or one of its bundled resource
  files via ``resource=``) → bumps ``views`` keyed on ``args['skill_name']``.
* ``write`` / ``edit`` with a ``.agents/skills/<name>/...`` path → bumps
  ``manages`` keyed on ``<name>``.

Cross-session lifecycle facts go through Memory Bank via the curator pass.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest


def _fake_context(state: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state if state is not None else {})


def _is_iso_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_callback_and_state_key():
    from horizon.memory.skill_telemetry import (
        SKILL_TELEMETRY_STATE_KEY,
        skill_telemetry_callback,
    )

    assert callable(skill_telemetry_callback)
    assert SKILL_TELEMETRY_STATE_KEY == "skill_telemetry"


# =============================================================================
# View counter — load_skill (instructions, and with resource=)
# =============================================================================


class TestSkillView:
    @pytest.mark.asyncio
    async def test_load_skill_increments_view_counter(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={"skill_name": "git-rebase"},
            tool_response={"skill_name": "git-rebase", "content": "..."},
            tool_context=ctx,
        )

        telem = ctx.state[SKILL_TELEMETRY_STATE_KEY]
        assert telem["git-rebase"]["views"] == 1
        assert telem["git-rebase"]["manages"] == 0
        assert _is_iso_timestamp(telem["git-rebase"]["last_used"])

    @pytest.mark.asyncio
    async def test_load_skill_with_resource_also_counts_as_view(self):
        """load_skill also covers what used to be a separate
        load_skill_resource tool — same tool name, an extra arg."""
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={
                "skill_name": "git-rebase",
                "resource": "references/notes.md",
            },
            tool_response={"skill_name": "git-rebase", "content": "..."},
            tool_context=ctx,
        )

        telem = ctx.state[SKILL_TELEMETRY_STATE_KEY]
        assert telem["git-rebase"]["views"] == 1

    @pytest.mark.asyncio
    async def test_view_multiple_times_accumulates(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        for _ in range(4):
            await skill_telemetry_callback(
                tool=SimpleNamespace(name="load_skill"),
                args={"skill_name": "git-rebase"},
                tool_response={"skill_name": "git-rebase"},
                tool_context=ctx,
            )

        assert ctx.state[SKILL_TELEMETRY_STATE_KEY]["git-rebase"]["views"] == 4

    @pytest.mark.asyncio
    async def test_view_failure_does_not_increment(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={"skill_name": "missing"},
            tool_response={"error": "Skill 'missing' not found."},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )


# =============================================================================
# Manage counter — write / edit on .agents/skills/<name>/...
# =============================================================================


class TestSkillManage:
    @pytest.mark.asyncio
    async def test_write_file_to_skill_path_bumps_manages(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="write"),
            args={
                "path": ".agents/skills/git-rebase/SKILL.md",
                "content": "...",
            },
            tool_response={
                "success": True,
                "path": ".agents/skills/git-rebase/SKILL.md",
            },
            tool_context=ctx,
        )

        telem = ctx.state[SKILL_TELEMETRY_STATE_KEY]
        assert telem["git-rebase"]["manages"] == 1
        assert telem["git-rebase"]["views"] == 0
        assert _is_iso_timestamp(telem["git-rebase"]["last_used"])

    @pytest.mark.asyncio
    async def test_patch_on_skill_path_bumps_manages(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="edit"),
            args={
                "path": ".agents/skills/git-rebase/SKILL.md",
                "old_string": "a",
                "new_string": "b",
            },
            tool_response={"success": True, "replacements": 1},
            tool_context=ctx,
        )

        assert (
            ctx.state[SKILL_TELEMETRY_STATE_KEY]["git-rebase"]["manages"] == 1
        )

    @pytest.mark.asyncio
    async def test_writes_to_skill_subresource_also_count(self):
        """Editing ``.agents/skills/<name>/references/foo.md`` is still a manage on
        ``<name>`` — sub-files are part of the skill."""
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="write"),
            args={
                "path": ".agents/skills/git-rebase/references/cheatsheet.md",
                "content": "...",
            },
            tool_response={"success": True},
            tool_context=ctx,
        )

        assert (
            ctx.state[SKILL_TELEMETRY_STATE_KEY]["git-rebase"]["manages"] == 1
        )

    @pytest.mark.asyncio
    async def test_manage_failure_does_not_increment(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="edit"),
            args={
                "path": ".agents/skills/git-rebase/SKILL.md",
                "edits": [{"oldText": "x", "newText": "y"}],
            },
            tool_response={"success": False, "error": "oldText not found."},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )

    @pytest.mark.asyncio
    async def test_writes_outside_skills_dir_are_ignored(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="write"),
            args={"path": "reports/q3.md", "content": "..."},
            tool_response={"success": True},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )

    @pytest.mark.asyncio
    async def test_write_to_skills_root_with_no_skill_name_is_ignored(self):
        """``write('.agents/skills/README.md', ...)`` has no ``<name>/`` after
        the prefix — there's no skill to attribute it to."""
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="write"),
            args={"path": ".agents/skills/README.md", "content": "..."},
            tool_response={"success": True},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )


# =============================================================================
# Cross-tool / cross-skill behaviour
# =============================================================================


class TestMultipleSkills:
    @pytest.mark.asyncio
    async def test_distinct_skills_tracked_separately(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={"skill_name": "skill-a"},
            tool_response={"skill_name": "skill-a"},
            tool_context=ctx,
        )
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="write"),
            args={"path": ".agents/skills/skill-b/SKILL.md", "content": "..."},
            tool_response={"success": True},
            tool_context=ctx,
        )

        telem = ctx.state[SKILL_TELEMETRY_STATE_KEY]
        assert telem["skill-a"]["views"] == 1
        assert telem["skill-a"]["manages"] == 0
        assert telem["skill-b"]["views"] == 0
        assert telem["skill-b"]["manages"] == 1

    @pytest.mark.asyncio
    async def test_view_then_manage_same_skill(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        for _ in range(2):
            await skill_telemetry_callback(
                tool=SimpleNamespace(name="load_skill"),
                args={"skill_name": "git-rebase"},
                tool_response={"skill_name": "git-rebase"},
                tool_context=ctx,
            )
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="edit"),
            args={
                "path": ".agents/skills/git-rebase/SKILL.md",
                "old_string": "a",
                "new_string": "b",
            },
            tool_response={"success": True, "replacements": 1},
            tool_context=ctx,
        )

        telem = ctx.state[SKILL_TELEMETRY_STATE_KEY]["git-rebase"]
        assert telem["views"] == 2
        assert telem["manages"] == 1


# =============================================================================
# Ignores unrelated tools
# =============================================================================


class TestNonSkillTools:
    @pytest.mark.asyncio
    async def test_unrelated_tool_ignored(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="read_file"),
            args={"path": "README.md"},
            tool_response={"success": True, "content": "..."},
            tool_context=ctx,
        )
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="bash"),
            args={"command": "ls"},
            tool_response={"exit_code": 0, "stdout": "foo"},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )

    @pytest.mark.asyncio
    async def test_reload_is_ignored(self):
        """``reload`` mutates the whole catalog (skills + extensions +
        manifest), not a single skill — no per-skill counter to bump."""
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="reload"),
            args={},
            tool_response={
                "success": True,
                "skills_loaded": [],
                "skills_removed": [],
            },
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )


# =============================================================================
# Defensive: missing/oddly shaped args
# =============================================================================


class TestDefensive:
    @pytest.mark.asyncio
    async def test_load_skill_missing_name_skips_quietly(self):
        from horizon.memory.skill_telemetry import (
            SKILL_TELEMETRY_STATE_KEY,
            skill_telemetry_callback,
        )

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={},
            tool_response={"success": True},
            tool_context=ctx,
        )

        assert (
            SKILL_TELEMETRY_STATE_KEY not in ctx.state
            or not ctx.state[SKILL_TELEMETRY_STATE_KEY]
        )

    @pytest.mark.asyncio
    async def test_none_args_skips_quietly(self):
        from horizon.memory.skill_telemetry import skill_telemetry_callback

        ctx = _fake_context()
        await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args=None,
            tool_response={"success": True},
            tool_context=ctx,
        )

    @pytest.mark.asyncio
    async def test_callback_returns_none(self):
        from horizon.memory.skill_telemetry import skill_telemetry_callback

        ctx = _fake_context()
        result = await skill_telemetry_callback(
            tool=SimpleNamespace(name="load_skill"),
            args={"skill_name": "x"},
            tool_response={"skill_name": "x"},
            tool_context=ctx,
        )
        assert result is None


class TestSkillNameFromPath:
    """The path parser that gates skill writes onto ``.agents/skills/<name>/``."""

    def test_canonical_agents_skills_path(self):
        from horizon.memory.skill_telemetry import skill_name_from_path

        assert skill_name_from_path(".agents/skills/foo/SKILL.md") == "foo"

    def test_leading_dot_slash_is_stripped_but_agents_dot_preserved(self):
        # ``lstrip('./')`` would eat the dot off ``.agents`` — regression guard.
        from horizon.memory.skill_telemetry import skill_name_from_path

        assert skill_name_from_path("./.agents/skills/foo/SKILL.md") == "foo"

    def test_bare_skills_path_no_longer_matches(self):
        from horizon.memory.skill_telemetry import skill_name_from_path

        assert skill_name_from_path("skills/foo/SKILL.md") is None

    def test_prefix_without_name_is_none(self):
        from horizon.memory.skill_telemetry import skill_name_from_path

        assert skill_name_from_path(".agents/skills/README.md") is None
