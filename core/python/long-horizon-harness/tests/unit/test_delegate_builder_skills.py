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

"""Delegate-builder skill catalog resolves through the session's host mirror.

The decisive case mirrors the W1 sandbox fix: a sandbox-style env whose
``working_dir`` does not exist on the host must still surface its user skills
to a delegated child, because they are served only through the async
``list_directory`` / ``read_file`` interface and mirrored to a host cache at
session start.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


async def test_render_skill_block_resolves_user_skill_from_host_mirror() -> (
    None
):
    from app.environment_context import (
        clear_active_environment,
        set_active_environment,
    )
    from app.subagents.delegate_builder import _render_skill_block
    from app.tools.skill_loader import mirror_user_skills_to_host
    from app.tools.skill_reload import host_mirror_dir

    interface_root = Path("/sandbox-only/delegate-ws")
    skills_root = interface_root / ".agents" / "skills"
    skill_dir = skills_root / "delegskill"
    skill_md = skill_dir / "SKILL.md"

    skill_body = (
        b"---\nname: delegskill\ndescription: An interface-only delegate skill.\n---\n"
        b"# delegskill\n\nRun `delegskill --do-the-thing` to proceed.\n"
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
                return ([{"name": "delegskill", "kind": "dir"}], False)
            if path == skill_dir:
                return ([{"name": "SKILL.md", "kind": "file"}], False)
            raise FileNotFoundError(str(path))

        async def read_file(self, path: Path) -> bytes:
            if Path(path) == skill_md:
                return skill_body
            raise FileNotFoundError(str(path))

    env = FakeEnv()
    set_active_environment(env)
    mirror_dir = host_mirror_dir(env.working_dir)
    try:
        await mirror_user_skills_to_host(env, dest_root=mirror_dir)
        block = await _render_skill_block("delegskill")
        assert "not found in the skill registry" not in block
        assert "delegskill --do-the-thing" in block
    finally:
        clear_active_environment()
        shutil.rmtree(mirror_dir, ignore_errors=True)
