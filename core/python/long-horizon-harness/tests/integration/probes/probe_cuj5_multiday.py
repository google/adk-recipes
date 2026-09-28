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

"""CUJ-5: Multi-day project continuity — state accumulates across N sessions.

Snapshot+restore must be durable through repeated round-trips, not just one.
The probe runs three sequential sessions, each one provisions from the prior
snapshot, appends a line to ``journal.md``, then closes. After the third,
provision a fourth read-only session and verify all three lines are present
in the exact order they were written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

JOURNAL = Path("/workspace/journal.md")
N_SESSIONS = 3


async def test_n_sessions_accumulate_state_durably(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
) -> None:
    from app.conversation import session_start
    from app.sandbox.provider import _build_sandbox_environment

    expected_lines: list[str] = []

    for i in range(N_SESSIONS):
        env, _ = _build_sandbox_environment(fresh_user_id)
        await env.initialize()
        try:
            # Read what's already there (empty on first iteration).
            try:
                existing = await env.read_file(JOURNAL)
            except FileNotFoundError:
                existing = b""

            line = f"day-{i}\n"
            expected_lines.append(line)
            await env.write_file(JOURNAL, existing + line.encode("utf-8"))
        finally:
            await env.close()

        # Force the next session to provision from the snapshot index, not
        # the now-closed in-process env.
        session_start._env_cache.clear()  # type: ignore[attr-defined]

    # Read-only verification session — must see all N lines in order.
    env, _ = _build_sandbox_environment(fresh_user_id)
    await env.initialize()
    try:
        final = (await env.read_file(JOURNAL)).decode("utf-8")
    finally:
        await env.close()

    assert final == "".join(expected_lines), (
        f"expected {expected_lines!r} got {final!r}"
    )
