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

"""CUJ-4: Extract a finished artifact via explicit ``artifact(action='save')``.

End-to-end test of the artifact dispatch tool: the agent writes a file in
the sandbox workspace, calls ``artifact(action='save', path=...)`` on it,
and a follow-up ``artifact(action='load', name=..., dest_path=...)`` gets
the exact same bytes back from the in-memory ``ArtifactService``. This is
the mechanism that lets the agent hand off a deliverable; if it breaks, the
workspace becomes a black hole.

The real ``GcsArtifactService`` path is covered by ADK's upstream tests —
we only test our wrapper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class _FakeToolContext:
    def __init__(self) -> None:
        self._store: dict[str, list] = {}

    async def save_artifact(self, *, filename: str, artifact) -> int:  # type: ignore[no-untyped-def]
        versions = self._store.setdefault(filename, [])
        versions.append(artifact)
        return len(versions) - 1

    async def load_artifact(self, *, filename: str, version: int | None = None):  # type: ignore[no-untyped-def]
        versions = self._store.get(filename)
        if not versions:
            return None
        return versions[-1 if version is None else version]


async def test_save_then_load_artifact_round_trip(
    fresh_user_id: str,
    isolated_local_root: Path,
    sandbox_env_vars: None,
    reset_env_cache: None,
) -> None:
    from app.environment_context import set_active_environment
    from app.sandbox.provider import _build_sandbox_environment
    from app.tools.artifacts import artifact

    payload = b"col1,col2\n1,2\n3,4\n"
    env, _ = _build_sandbox_environment(fresh_user_id)
    await env.initialize()
    set_active_environment(env)

    try:
        await env.write_file(Path("/workspace/report.csv"), payload)

        ctx = _FakeToolContext()
        save_result = await artifact(
            action="save", path="/workspace/report.csv", tool_context=ctx
        )
        assert save_result["success"] is True
        assert save_result["filename"] == "report.csv"

        load_result = await artifact(
            action="load",
            name="report.csv",
            dest_path="/workspace/copy.csv",
            tool_context=ctx,
        )
        assert load_result["success"] is True

        copied = await env.read_file(Path("/workspace/copy.csv"))
        assert copied == payload
    finally:
        await env.close()
