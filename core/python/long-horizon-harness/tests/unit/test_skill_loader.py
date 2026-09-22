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

"""Loader for ADK ``SkillToolset`` — pins discovery + dual-root shadowing.

``horizon.tools.skill_loader`` is the boundary between the user-skill filesystem
layout (user skills under ``<workspace>/.agents/skills/<name>/SKILL.md``;
shipped builtins under ``horizon/builtin_skills/<name>/SKILL.md``) and
ADK's experimental ``SkillToolset``. The walker returns a single
``dict[str, models.Skill]`` with user skills shadowing builtins by
``name``; the factory wraps that into a toolset with ``ListSkillsTool``
dropped so ADK auto-injects the ``<available_skills>`` catalog.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

_USER_SKILL_MD = (
    "---\n"
    "name: alpha\n"
    "description: a user-defined alpha skill\n"
    "---\n"
    "# alpha\n\nbody from user dir\n"
)

_USER_SHADOW_MD = (
    "---\n"
    "name: shared\n"
    "description: user override of shared skill\n"
    "---\n"
    "# user version\n\nuser body\n"
)

_BUILTIN_SHARED_MD = (
    "---\n"
    "name: shared\n"
    "description: builtin shared skill\n"
    "---\n"
    "# builtin version\n\nbuiltin body\n"
)

_BUILTIN_ONLY_MD = (
    "---\n"
    "name: bravo\n"
    "description: builtin-only bravo skill\n"
    "---\n"
    "# bravo\n\nbuiltin body\n"
)


def _write_skill(root: Path, name: str, content: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def skill_roots(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    user = tmp_path / "user"
    builtin = tmp_path / "builtin"
    user.mkdir()
    builtin.mkdir()
    yield user, builtin


# =============================================================================
# walk_skill_dirs
# =============================================================================


class TestWalkSkillDirs:
    def test_returns_dict_keyed_by_name(self, skill_roots: tuple[Path, Path]):
        from horizon.tools.skill_loader import walk_skill_dirs

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        result = walk_skill_dirs(user_dir=user, builtin_dir=builtin)

        assert isinstance(result, dict)
        assert "alpha" in result
        # Value shape must be ADK's ``models.Skill``.
        from google.adk.skills import models

        assert isinstance(result["alpha"], models.Skill)

    def test_includes_user_skills(self, skill_roots: tuple[Path, Path]):
        from horizon.tools.skill_loader import walk_skill_dirs

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        result = walk_skill_dirs(user_dir=user, builtin_dir=builtin)

        assert "alpha" in result
        assert result["alpha"].frontmatter.name == "alpha"

    def test_includes_builtin_skills(self, skill_roots: tuple[Path, Path]):
        from horizon.tools.skill_loader import walk_skill_dirs

        user, builtin = skill_roots
        _write_skill(builtin, "bravo", _BUILTIN_ONLY_MD)

        result = walk_skill_dirs(user_dir=user, builtin_dir=builtin)

        assert "bravo" in result
        assert result["bravo"].frontmatter.name == "bravo"

    def test_user_shadows_builtin_on_name_collision(
        self, skill_roots: tuple[Path, Path]
    ):
        from horizon.tools.skill_loader import walk_skill_dirs

        user, builtin = skill_roots
        _write_skill(user, "shared", _USER_SHADOW_MD)
        _write_skill(builtin, "shared", _BUILTIN_SHARED_MD)

        result = walk_skill_dirs(user_dir=user, builtin_dir=builtin)

        assert "shared" in result
        # The user body wins.
        assert "user body" in result["shared"].instructions
        assert "builtin body" not in result["shared"].instructions

    def test_missing_user_dir_falls_back_to_builtin(self, tmp_path: Path):
        from horizon.tools.skill_loader import walk_skill_dirs

        builtin = tmp_path / "builtin"
        builtin.mkdir()
        _write_skill(builtin, "bravo", _BUILTIN_ONLY_MD)

        result = walk_skill_dirs(
            user_dir=tmp_path / "user_does_not_exist", builtin_dir=builtin
        )

        assert "bravo" in result

    def test_missing_builtin_dir_returns_only_user(self, tmp_path: Path):
        from horizon.tools.skill_loader import walk_skill_dirs

        user = tmp_path / "user"
        user.mkdir()
        _write_skill(user, "alpha", _USER_SKILL_MD)

        result = walk_skill_dirs(
            user_dir=user, builtin_dir=tmp_path / "builtin_missing"
        )

        assert "alpha" in result
        assert len(result) == 1

    def test_both_dirs_missing_returns_empty(self, tmp_path: Path):
        from horizon.tools.skill_loader import walk_skill_dirs

        result = walk_skill_dirs(
            user_dir=tmp_path / "u", builtin_dir=tmp_path / "b"
        )

        assert result == {}

    def test_malformed_skill_skipped_without_raising(
        self, skill_roots: tuple[Path, Path]
    ):
        """A bad SKILL.md must not poison the rest of the catalog."""
        from horizon.tools.skill_loader import walk_skill_dirs

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)
        _write_skill(user, "broken", "not yaml frontmatter at all")

        result = walk_skill_dirs(user_dir=user, builtin_dir=builtin)

        assert "alpha" in result
        assert "broken" not in result


# =============================================================================
# build_skill_toolset
# =============================================================================


class TestBuildSkillToolset:
    def test_returns_skill_toolset(self, skill_roots: tuple[Path, Path]):
        from google.adk.tools.skill_toolset import SkillToolset

        from horizon.tools.skill_loader import build_skill_toolset

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        toolset = build_skill_toolset(user_dir=user, builtin_dir=builtin)

        assert isinstance(toolset, SkillToolset)

    def test_drops_list_skills_tool(self, skill_roots: tuple[Path, Path]):
        """Mode B from the spike — no ListSkillsTool so ADK auto-injects
        the ``<available_skills>`` XML catalog into the system prompt."""
        from google.adk.tools.skill_toolset import ListSkillsTool

        from horizon.tools.skill_loader import build_skill_toolset

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        toolset = build_skill_toolset(user_dir=user, builtin_dir=builtin)

        assert not any(isinstance(t, ListSkillsTool) for t in toolset._tools)

    def test_keeps_merged_load_skill_tool(self, skill_roots: tuple[Path, Path]):
        """ADK's own LoadSkillTool / LoadSkillResourceTool / RunSkillScriptTool
        trio is replaced with horizon's merged load_skill (Task 9 cut E):
        load_skill(skill_name) for instructions, load_skill(skill_name,
        resource=...) for a bundled file. run_skill_script has no successor.
        """
        from google.adk.tools.skill_toolset import (
            LoadSkillResourceTool,
            RunSkillScriptTool,
        )
        from google.adk.tools.skill_toolset import (
            LoadSkillTool as AdkLoadSkillTool,
        )

        from horizon.tools.skill_loader import build_skill_toolset
        from horizon.tools.skill_toolset import (
            LoadSkillTool as HorizonLoadSkillTool,
        )

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        toolset = build_skill_toolset(user_dir=user, builtin_dir=builtin)

        types_present = {type(t) for t in toolset._tools}
        assert HorizonLoadSkillTool in types_present
        assert AdkLoadSkillTool not in types_present
        assert LoadSkillResourceTool not in types_present
        assert RunSkillScriptTool not in types_present
        assert {t.name for t in toolset._tools} == {"load_skill"}

    @pytest.mark.asyncio
    async def test_process_llm_request_injects_catalog(
        self, skill_roots: tuple[Path, Path]
    ):
        """With ListSkillsTool removed, ADK injects ``<available_skills>``
        into the system prompt every turn — the in-prompt index for free.
        HorizonSkillToolset.process_llm_request reads llm_request.config
        directly (the delta-safety mechanism from Task 8), so this needs a
        real LlmRequest rather than a hand-rolled append_instructions-only
        stub."""
        from google.adk.models import LlmRequest

        from horizon.tools.skill_loader import build_skill_toolset

        user, builtin = skill_roots
        _write_skill(user, "alpha", _USER_SKILL_MD)

        toolset = build_skill_toolset(user_dir=user, builtin_dir=builtin)

        req = LlmRequest()
        await toolset.process_llm_request(tool_context=None, llm_request=req)

        merged = req.config.system_instruction or ""
        assert "<available_skills>" in merged
        assert "alpha" in merged


# =============================================================================
# Built-in surface migrated off horizon.tools.skills
# =============================================================================


class TestModuleSurface:
    def test_builtin_root_points_at_app_builtin_skills(self):
        """The loader owns the host-side builtin root that ``skills.py``
        used to provide via ``_builtin_root()``."""
        from horizon.tools.skill_loader import builtin_skills_root

        root = builtin_skills_root()
        assert root.is_dir()
        assert root.name == "builtin_skills"
        assert (root / "policy" / "SKILL.md").is_file()
