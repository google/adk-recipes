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

import pytest

from app.conversation import session_start
from app.environment import LocalEnvironment
from app.routines.run_context import (
    RoutineRun,
    reset_routine_run,
    set_routine_run,
)
from app.sandbox import provider


@pytest.fixture(autouse=True)
def _reset_caches():
    session_start._env_cache.clear()
    session_start._provisioning_locks.clear()
    yield
    session_start._env_cache.clear()
    session_start._provisioning_locks.clear()


@pytest.mark.asyncio
async def test_local_routine_env_is_routine_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setattr(provider, "_default_local_root", lambda: tmp_path)

    token = set_routine_run(RoutineRun(routine_id="digest", owner="alice@x"))
    try:
        env = await session_start._ensure_environment("alice@x")
    finally:
        reset_routine_run(token)

    assert isinstance(env, LocalEnvironment)
    # routine dir, NOT the user's (.../users/alice@x)
    assert env.working_dir == tmp_path / "routines" / "digest"
    # cached under the routine key, never the user key
    assert ("routine", "digest") in session_start._env_cache
    assert ("local", "alice@x") not in session_start._env_cache


@pytest.mark.asyncio
async def test_user_env_unaffected_when_no_routine_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setattr(provider, "_default_local_root", lambda: tmp_path)

    env = await session_start._ensure_environment("alice@x")

    assert env.working_dir == tmp_path / "users" / "alice@x"
    assert ("local", "alice@x") in session_start._env_cache
    assert ("routine", "digest") not in session_start._env_cache


@pytest.mark.asyncio
async def test_routine_and_user_envs_are_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setattr(provider, "_default_local_root", lambda: tmp_path)

    user_env = await session_start._ensure_environment("alice@x")
    token = set_routine_run(RoutineRun(routine_id="digest", owner="alice@x"))
    try:
        routine_env = await session_start._ensure_environment("alice@x")
    finally:
        reset_routine_run(token)

    assert routine_env is not user_env
    assert routine_env.working_dir == tmp_path / "routines" / "digest"
    assert user_env.working_dir == tmp_path / "users" / "alice@x"
