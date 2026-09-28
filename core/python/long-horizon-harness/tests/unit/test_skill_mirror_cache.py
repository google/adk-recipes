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

"""bind_session_skills_callback mirrors per user once, not every turn.

The env-interface mirror is a sandbox round-trip; doing it on every turn was the
dominant per-turn latency. It is cached per user (owner + working_dir), and in
sandbox mode every user shares ``/workspace`` so the owner is what keeps one
user's skills from bleeding into another's cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.environment_context import (
    clear_active_environment,
    set_active_environment,
)
from app.tools import skill_reload
from app.tools.skill_loader import build_skill_toolset

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}


def _make_fake_mirror(calls: list[tuple[str | None, Path]]):
    async def fake_mirror(env, *, dest_root: Path) -> Path:
        owner = getattr(env, "owner", None)
        calls.append((owner, dest_root))
        skill_dir = dest_root / f"skill-{owner}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: skill-{owner}\ndescription: for {owner}.\n---\nbody\n"
        )
        return dest_root

    return fake_mirror


async def _bind_for(env) -> None:
    set_active_environment(env)
    try:
        await skill_reload.bind_session_skills_callback(_Ctx())
    finally:
        clear_active_environment()


async def test_mirror_runs_once_per_user(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str | None, Path]] = []
    monkeypatch.setattr(
        skill_reload, "mirror_user_skills_to_host", _make_fake_mirror(calls)
    )

    class FakeEnv:
        working_dir = Path("/workspace")
        owner = "solo"

    toolset = build_skill_toolset(user_dir=tmp_path / "unused")
    skill_reload.bind_toolset(
        toolset,
        user_dir=tmp_path / "unused",
        builtin_dir=tmp_path / "no-builtin",
    )
    try:
        await _bind_for(FakeEnv())
        await _bind_for(FakeEnv())
        assert len(calls) == 1  # second turn reuses the cached mirror
        assert "skill-solo" in toolset._skills
    finally:
        skill_reload.unbind_toolset()


async def test_mirror_reruns_and_isolates_per_owner(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[tuple[str | None, Path]] = []
    monkeypatch.setattr(
        skill_reload, "mirror_user_skills_to_host", _make_fake_mirror(calls)
    )

    class FakeEnvA:
        working_dir = Path("/workspace")
        owner = "alice"

    class FakeEnvB:
        working_dir = Path("/workspace")  # same in-container path as A
        owner = "bob"

    toolset = build_skill_toolset(user_dir=tmp_path / "unused")
    skill_reload.bind_toolset(
        toolset,
        user_dir=tmp_path / "unused",
        builtin_dir=tmp_path / "no-builtin",
    )
    try:
        await _bind_for(FakeEnvA())
        await _bind_for(FakeEnvB())
        await _bind_for(FakeEnvA())  # cached — no re-mirror

        assert (
            len(calls) == 2
        )  # one per distinct owner, despite shared /workspace
        # After rebinding A, only A's skill is live — no bleed from bob.
        assert "skill-alice" in toolset._skills
        assert "skill-bob" not in toolset._skills
    finally:
        skill_reload.unbind_toolset()
