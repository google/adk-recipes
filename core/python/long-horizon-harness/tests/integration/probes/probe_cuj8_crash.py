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

"""CUJ-8: Crash recovery — live sandbox survives an unclean process drop.

Simulates a crash by dropping the env reference WITHOUT calling
``close()``, then asserts the platform-side sandbox is still
``STATE_RUNNING`` and a second session reattaches to it with the
sentinel intact.

Under the persistent-sandbox model the sandbox lives independently of
the Python process — there is no snapshot/atexit dance to validate.
The interesting failure mode is "did we leak a sandbox that vanished
on process exit" — proven false by ``sandboxes.get`` returning
RUNNING after the simulated crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

SENTINEL_PATH = Path("/workspace/crash_sentinel.txt")


async def test_live_sandbox_survives_unclean_drop(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
    vertex_client: object,
) -> None:
    from app.conversation import session_start
    from app.sandbox.provider import _build_sandbox_environment

    sentinel = b"crashed-but-still-running"

    # Session 1: provision + write, drop ref WITHOUT close.
    env, _ = _build_sandbox_environment(fresh_user_id)
    await env.initialize()
    await env.write_file(SENTINEL_PATH, sentinel)
    sandbox_name = env.sandbox_name  # type: ignore[attr-defined]
    session_start._env_cache[("sandbox", fresh_user_id)] = env  # type: ignore[attr-defined]
    del env  # the "crash"

    # The sandbox is independent of the process — it must still be
    # RUNNING despite the unclean drop.
    sandbox_obj = vertex_client.agent_engines.sandboxes.get(  # type: ignore[attr-defined]
        name=sandbox_name
    )
    state = getattr(sandbox_obj, "state", None)
    state_name = getattr(state, "name", None) or str(state)
    assert "RUNNING" in state_name, (
        f"sandbox {sandbox_name} should be RUNNING after unclean drop, "
        f"got state={state_name}"
    )

    # Session 2: reattach via the index and confirm the sentinel survived.
    session_start._env_cache.clear()  # type: ignore[attr-defined]
    env2, _ = _build_sandbox_environment(fresh_user_id)
    await env2.initialize()
    try:
        observed = await env2.read_file(SENTINEL_PATH)
        assert observed == sentinel
        assert env2.sandbox_name == sandbox_name  # type: ignore[attr-defined]
    finally:
        await env2.close()
