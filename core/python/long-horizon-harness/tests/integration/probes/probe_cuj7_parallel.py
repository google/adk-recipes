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

"""CUJ-7: Parallel sessions for same ``user_id`` — both succeed, one wins.

Two ``_build_sandbox_environment(user_id)`` calls executed concurrently with
``asyncio.gather`` must both come back with a working sandbox (no Vertex
rejection, no race in the index helpers). Both write a sentinel; after both
close, a third session reads the snapshot and must observe ONE of the
sentinels — last-writer-wins is acceptable, both losing is not.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

SENTINEL_PATH = Path("/workspace/last_writer.txt")


async def _session(fresh_user_id: str, sentinel: bytes) -> None:
    from app.sandbox.provider import _build_sandbox_environment

    env, _ = _build_sandbox_environment(fresh_user_id)
    await env.initialize()
    try:
        await env.write_file(SENTINEL_PATH, sentinel)
    finally:
        await env.close()


async def test_two_parallel_sessions_both_succeed(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
) -> None:
    from app.conversation import session_start
    from app.sandbox.provider import _build_sandbox_environment

    sentinel_a = b"writer-A"
    sentinel_b = b"writer-B"

    # Both must complete without raising.
    await asyncio.gather(
        _session(fresh_user_id, sentinel_a),
        _session(fresh_user_id, sentinel_b),
    )

    session_start._env_cache.clear()  # type: ignore[attr-defined]

    # Third session: read what survived.
    env, _ = _build_sandbox_environment(fresh_user_id)
    await env.initialize()
    try:
        observed = await env.read_file(SENTINEL_PATH)
    finally:
        await env.close()

    # Last-writer-wins — one of the two must survive.
    assert observed in (sentinel_a, sentinel_b), (
        f"expected one of {sentinel_a!r} or {sentinel_b!r}, got {observed!r}"
    )
