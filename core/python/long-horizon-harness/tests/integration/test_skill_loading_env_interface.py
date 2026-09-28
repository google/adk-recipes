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

"""Skill loading flows through the environment interface, not raw host pathlib.

The decisive case is ``test_skills_load_when_workspace_is_not_on_host``:
a sandbox-style env whose ``working_dir`` does not exist on the host must
still surface its user skills, because they are served only through the
async ``list_directory`` / ``read_file`` interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.environment import LocalEnvironment
from app.environment_context import (
    clear_active_environment,
    set_active_environment,
)
from app.tools import skill_reload
from app.tools.skill_loader import (
    build_skill_toolset,
    mirror_user_skills_to_host,
)


def _write_skill(root: Path, name: str) -> None:
    # Skills live under .agents/skills/<name>/ — the npx-add staging location.
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A {name} skill for testing.\n---\n"
        f"# {name}\n\nBody for {name}.\n"
    )


@pytest.mark.asyncio
async def test_agents_skills_dir_is_discovered(tmp_path: Path) -> None:
    """Skills staged under ``.agents/skills/`` (the npx-add location) mirror."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(workspace, "npxskill")

    env = LocalEnvironment(working_dir=workspace)
    dest = tmp_path / "mirror"
    try:
        await mirror_user_skills_to_host(env, dest_root=dest)
        assert (dest / "npxskill" / "SKILL.md").exists()
    finally:
        clear_active_environment()


class _DummyCtx:
    pass


@pytest.mark.asyncio
async def test_user_skills_load_through_env_interface(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(workspace, "probe")

    env = LocalEnvironment(working_dir=workspace)
    set_active_environment(env)
    toolset = build_skill_toolset(user_dir=workspace / ".agents" / "skills")
    skill_reload.bind_toolset(
        toolset,
        user_dir=workspace / ".agents" / "skills",
        builtin_dir=workspace / "nonexistent-builtin",
    )
    try:
        await skill_reload.bind_session_skills_callback(_DummyCtx())
        assert "probe" in toolset._skills
    finally:
        skill_reload.unbind_toolset()
        clear_active_environment()


@pytest.mark.asyncio
async def test_mirror_user_skills_to_host(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(workspace, "alpha")
    refs = workspace / ".agents" / "skills" / "alpha" / "references"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "ref.md").write_text("# reference\n\nshared notes\n")

    env = LocalEnvironment(working_dir=workspace)
    dest = tmp_path / "mirror"
    try:
        result = await mirror_user_skills_to_host(env, dest_root=dest)
        assert result == dest
        skill_md = dest / "alpha" / "SKILL.md"
        ref_md = dest / "alpha" / "references" / "ref.md"
        assert skill_md.exists()
        assert ref_md.exists()
        assert "name: alpha" in skill_md.read_text()
        assert "shared notes" in ref_md.read_text()
    finally:
        clear_active_environment()


@pytest.mark.asyncio
async def test_skills_load_when_workspace_is_not_on_host(
    tmp_path: Path,
) -> None:
    interface_root = Path("/sandbox-only/workspace")
    skills_root = interface_root / ".agents" / "skills"
    skill_dir = skills_root / "interfaceskill"
    skill_md = skill_dir / "SKILL.md"

    skill_body = (
        b"---\nname: interfaceskill\ndescription: An interface-only skill.\n---\n"
        b"# interfaceskill\n\nServed only through the env interface.\n"
    )

    class FakeEnv:
        @property
        def working_dir(self) -> Path:
            return interface_root

        async def list_directory(
            self, path: Path, *, limit: int
        ) -> tuple[list[dict[str, Any]], bool]:
            path = Path(path)
            if path == skills_root:
                return ([{"name": "interfaceskill", "kind": "dir"}], False)
            if path == skill_dir:
                return ([{"name": "SKILL.md", "kind": "file"}], False)
            raise FileNotFoundError(str(path))

        async def read_file(self, path: Path) -> bytes:
            if Path(path) == skill_md:
                return skill_body
            raise FileNotFoundError(str(path))

    set_active_environment(FakeEnv())
    toolset = build_skill_toolset(user_dir=tmp_path / "unused")
    skill_reload.bind_toolset(
        toolset,
        user_dir=tmp_path / "unused",
        builtin_dir=tmp_path / "nonexistent-builtin",
    )
    try:
        await skill_reload.bind_session_skills_callback(_DummyCtx())
        assert "interfaceskill" in toolset._skills
    finally:
        skill_reload.unbind_toolset()
        clear_active_environment()


@pytest.mark.asyncio
async def test_symlink_entries_are_skipped_without_aborting_mirror(
    tmp_path: Path,
) -> None:
    interface_root = Path("/sandbox-only/symlink-ws")
    skills_root = interface_root / ".agents" / "skills"
    good_dir = skills_root / "good"
    good_md = good_dir / "SKILL.md"
    linked_path = skills_root / "linked"

    good_body = (
        b"---\nname: good\ndescription: A normal skill.\n---\n"
        b"# good\n\nNormal skill body.\n"
    )

    class FakeEnv:
        @property
        def working_dir(self) -> Path:
            return interface_root

        async def list_directory(
            self, path: Path, *, limit: int
        ) -> tuple[list[dict[str, Any]], bool]:
            path = Path(path)
            if path == skills_root:
                return (
                    [
                        {"name": "linked", "kind": "symlink"},
                        {"name": "good", "kind": "dir"},
                    ],
                    False,
                )
            if path == good_dir:
                return ([{"name": "SKILL.md", "kind": "file"}], False)
            raise FileNotFoundError(str(path))

        async def read_file(self, path: Path) -> bytes:
            if Path(path) == good_md:
                return good_body
            # A symlink read raises PermissionError, NOT FileNotFoundError, so
            # if the symlink-skip branch were removed the mirror would attempt
            # this read and fail loudly (the per-entry guard only swallows
            # FileNotFoundError). This isolates the skip branch as under test.
            if Path(path) == linked_path:
                raise PermissionError(str(path))
            raise FileNotFoundError(str(path))

    set_active_environment(FakeEnv())
    dest = tmp_path / "mirror"
    try:
        await mirror_user_skills_to_host(FakeEnv(), dest_root=dest)
        assert (dest / "good" / "SKILL.md").exists()
        assert not (dest / "linked").exists()
    finally:
        clear_active_environment()


@pytest.mark.asyncio
async def test_removed_skill_disappears_after_resync(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(workspace, "keep")
    _write_skill(workspace, "drop")

    env = LocalEnvironment(working_dir=workspace)
    set_active_environment(env)
    mirror_dir = tmp_path / "mirror"
    toolset = build_skill_toolset(user_dir=mirror_dir)
    skill_reload.bind_toolset(
        toolset,
        user_dir=mirror_dir,
        builtin_dir=tmp_path / "nonexistent-builtin",
    )
    try:
        await skill_reload.bind_session_skills_callback(_DummyCtx())
        assert {"keep", "drop"} <= set(toolset._skills)

        skill_reload.bind_toolset(
            toolset,
            user_dir=mirror_dir,
            builtin_dir=tmp_path / "nonexistent-builtin",
        )
        import shutil as _shutil

        _shutil.rmtree(workspace / ".agents" / "skills" / "drop")
        await mirror_user_skills_to_host(env, dest_root=mirror_dir)
        skill_reload.refresh_skills()

        assert "keep" in toolset._skills
        assert "drop" not in toolset._skills
    finally:
        skill_reload.unbind_toolset()
        clear_active_environment()


@pytest.mark.asyncio
async def test_missing_skills_dir_yields_empty_existing_mirror(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    env = LocalEnvironment(working_dir=workspace)
    dest = tmp_path / "mirror"
    try:
        result = await mirror_user_skills_to_host(env, dest_root=dest)
        assert result == dest
        assert dest.is_dir()
        assert list(dest.iterdir()) == []
    finally:
        clear_active_environment()
