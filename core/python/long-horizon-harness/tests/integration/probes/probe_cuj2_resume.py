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

"""CUJ-2: Returning user resumes — ``/workspace`` is exactly as left.

Two sequential sessions for the same ``user_id``. Session 1 provisions a
sandbox, writes a sentinel, and closes (the platform-side sandbox stays
RUNNING — ``close()`` only tears down the local HTTP client). Session 2
reattaches via Vertex's ``sandboxes.list`` + display_name discovery and
must see the sentinel — proves the persistent-sandbox reattach loop works
across process boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_session_2_reattaches_to_session_1_sandbox(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
) -> None:
    import os

    from app.conversation import session_start

    # These helpers no longer exist in session_start (provisioning now goes
    # through _resolve_provider); this live probe needs porting to that API.
    from app.conversation.session_start import (  # type: ignore[attr-defined]
        _build_sandbox_environment,
        _resolve_sandbox_engine,
        _vertex_client_factory,
    )
    from app.sandbox.lifecycle import find_user_sandbox

    sentinel = b"lha-cuj2-resume-sentinel"
    notes_path = Path("/workspace/notes.md")
    image_uri = os.environ.get("LHA_RUNTIME_IMAGE", "").strip()

    env1, _ = _build_sandbox_environment(fresh_user_id)
    await env1.initialize()
    try:
        await env1.write_file(notes_path, sentinel)
    finally:
        await env1.close()

    client = _vertex_client_factory()
    agent_instance = _resolve_sandbox_engine(client)
    discovered = find_user_sandbox(
        client,
        agent_instance_name=agent_instance,
        user_id=fresh_user_id,
        image_uri=image_uri,
    )
    assert discovered, "session 1 sandbox not discoverable via sandboxes.list"

    # Drop the in-process cache so session 2 goes through the reattach
    # path rather than handing back the already-closed env1.
    session_start._env_cache.clear()  # type: ignore[attr-defined]

    env2, _ = _build_sandbox_environment(fresh_user_id)
    await env2.initialize()
    try:
        out = await env2.read_file(notes_path)
        assert out == sentinel
        # Same platform-side sandbox, not a fresh provision.
        assert env2.sandbox_name == discovered
    finally:
        await env2.close()
