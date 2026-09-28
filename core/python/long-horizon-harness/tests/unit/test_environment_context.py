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

"""ContextVar-backed per-session ``BaseEnvironment`` registry.

Skills code and (eventually) tool refactors look up the current
environment via ``active_environment()`` instead of building one
themselves. ``before_agent_callback`` installs the env at session
start; tests install one via the same setter so they don't need any
ADK runtime to exercise skill IO.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_active_environment_raises_when_unset() -> None:
    from app.environment_context import (
        active_environment,
        clear_active_environment,
    )

    clear_active_environment()
    with pytest.raises(RuntimeError):
        active_environment()


def test_set_and_get_active_environment(tmp_path: Path) -> None:
    from app.environment import LocalEnvironment
    from app.environment_context import (
        active_environment,
        clear_active_environment,
        set_active_environment,
    )

    clear_active_environment()
    env = LocalEnvironment(working_dir=tmp_path)
    set_active_environment(env)
    try:
        assert active_environment() is env
    finally:
        clear_active_environment()


def test_clear_active_environment_unsets(tmp_path: Path) -> None:
    from app.environment import LocalEnvironment
    from app.environment_context import (
        active_environment,
        clear_active_environment,
        set_active_environment,
    )

    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    clear_active_environment()
    with pytest.raises(RuntimeError):
        active_environment()


def test_active_environment_is_isolated_across_async_tasks(
    tmp_path: Path,
) -> None:
    """ContextVar isolates per-task state — concurrent sessions don't
    leak each other's environments."""
    from app.environment import LocalEnvironment
    from app.environment_context import (
        active_environment,
        clear_active_environment,
        set_active_environment,
    )

    clear_active_environment()

    async def task(working_dir: Path) -> Path:
        set_active_environment(LocalEnvironment(working_dir=working_dir))
        await asyncio.sleep(0)
        return active_environment().working_dir

    async def driver() -> tuple[Path, Path]:
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        return await asyncio.gather(task(dir_a), task(dir_b))

    a, b = asyncio.run(driver())
    assert a != b
    assert a.name == "a"
    assert b.name == "b"

    # Outer context was untouched by the inner tasks (ContextVar isolation).
    with pytest.raises(RuntimeError):
        active_environment()
