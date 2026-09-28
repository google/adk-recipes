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

"""The bound skill catalog is mirrored into ``session.state`` so the web
Skills panel can list installed skills before they're ever invoked.

Local backend only (the autouse ``_scoped_environment`` fixture binds a
``LocalEnvironment``); no Vertex, no LLM assertions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.environment_context import set_active_environment
from app.tools import skill_reload
from app.tools.skill_loader import build_skill_toolset, builtin_skills_root

_USER_SKILL_MD = (
    "---\n"
    "name: my-changelog\n"
    "description: keep a running changelog\n"
    "---\n"
    "# my-changelog\n\nbody\n"
)

_USER_POLICY_MD = (
    "---\n"
    "name: policy\n"
    "description: user override of the policy skill\n"
    "---\n"
    "# policy\n\nuser body\n"
)


def _write_skill(root: Path, name: str, content: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class _Ctx:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}


class _ToolCtx:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}


@pytest.fixture
def workspace(tmp_path: Path):
    """A LocalEnvironment-rooted workspace with a real builtin catalog and a
    freshly-created (never invoked) user skill."""
    from app.environment import LocalEnvironment

    ws = tmp_path / "ws"
    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    _write_skill(skills_dir, "my-changelog", _USER_SKILL_MD)
    set_active_environment(LocalEnvironment(working_dir=ws))

    toolset = build_skill_toolset(
        user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    skill_reload.bind_toolset(
        toolset, user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    try:
        yield ws
    finally:
        skill_reload.unbind_toolset()


@pytest.mark.asyncio
async def test_bind_writes_catalog_with_uninvoked_user_and_builtin(
    workspace: Path,
) -> None:
    ctx = _Ctx()

    await skill_reload.bind_session_skills_callback(ctx)

    catalog = ctx.state[skill_reload.BOUND_SKILLS_STATE_KEY]
    by_name = {row["name"]: row for row in catalog}

    assert "my-changelog" in by_name
    assert by_name["my-changelog"]["source"] == "user"
    # A real built-in (policy) is present without any invocation.
    assert "policy" in by_name
    assert by_name["policy"]["source"] == "builtin"


@pytest.mark.asyncio
async def test_catalog_rows_carry_name_and_description(workspace: Path) -> None:
    ctx = _Ctx()

    await skill_reload.bind_session_skills_callback(ctx)

    row = next(
        r
        for r in ctx.state[skill_reload.BOUND_SKILLS_STATE_KEY]
        if r["name"] == "my-changelog"
    )
    assert row["description"] == "keep a running changelog"


@pytest.mark.asyncio
async def test_user_skill_shadowing_builtin_tagged_user(tmp_path: Path) -> None:
    from app.environment import LocalEnvironment

    ws = tmp_path / "ws"
    skills_dir = ws / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    _write_skill(skills_dir, "policy", _USER_POLICY_MD)
    set_active_environment(LocalEnvironment(working_dir=ws))

    toolset = build_skill_toolset(
        user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    skill_reload.bind_toolset(
        toolset, user_dir=skills_dir, builtin_dir=builtin_skills_root()
    )
    try:
        ctx = _Ctx()
        await skill_reload.bind_session_skills_callback(ctx)
        by_name = {
            r["name"]: r for r in ctx.state[skill_reload.BOUND_SKILLS_STATE_KEY]
        }
        assert by_name["policy"]["source"] == "user"
    finally:
        skill_reload.unbind_toolset()


@pytest.mark.asyncio
async def test_reload_tool_writes_catalog_into_tool_context_state(
    workspace: Path,
) -> None:
    from app.commands import reload

    tool_ctx = _ToolCtx()
    await reload(tool_context=tool_ctx)

    catalog = tool_ctx.state[skill_reload.BOUND_SKILLS_STATE_KEY]
    names = {row["name"] for row in catalog}
    assert "my-changelog" in names
    assert "policy" in names
