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

"""CUJ-1: Ephemeral exploration — fresh user, no prior state, nothing persists.

Pins: a session that never opts in to a persistent identity gets cleaned up.
Specifically, after ``close()`` the sandbox resource is gone (the snapshot
index does pick up the throwaway user's snapshot because ``close()`` always
snapshots — but the resource itself must not linger and burn quota).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_ephemeral_session_cleans_up_sandbox(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
    vertex_client: object,
) -> None:
    from app.environment.sandbox import SandboxEnvironment
    from app.sandbox.provider import _build_sandbox_environment

    env, _ = _build_sandbox_environment(fresh_user_id)
    assert isinstance(env, SandboxEnvironment)
    sandbox_name = env.sandbox_name
    await env.initialize()

    try:
        await env.write_file(Path("/workspace/scratch.txt"), b"ephemeral")
        out = await env.read_file(Path("/workspace/scratch.txt"))
        assert out == b"ephemeral"
    finally:
        await env.close()

    # Sandbox should be gone post-close. Use the SDK's get to confirm 404.
    from google.api_core import (
        exceptions as gcp_exc,  # type: ignore[import-not-found]
    )

    with pytest.raises((gcp_exc.NotFound, Exception)):
        vertex_client.agent_engines.sandboxes.get(name=sandbox_name)  # type: ignore[attr-defined]
