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

"""Tests for the ``bash`` tool: ``bash(command, timeout_s)``, a minimal
parameter space. No ``cwd`` (use ``cd dir && cmd``), no
``background``/``on_timeout``:
background spawn moved to ``process(action='spawn', ...)``
(tests/unit/tools/processes/test_process_tool.py::TestSpawnAction).

A command still running past ``timeout_s`` auto-promotes to a background
session (partial output preserved) — this is now the ONLY behavior, not a
choice, so what used to be ``TestAutoPromote``'s "default" case is now the
only case.

Per-operation approval is handled centrally by ``permission_guard`` in the
``before_tool_callback`` chain, so the wrapper itself never gates in-body —
it just runs the command.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from horizon.environment import LocalEnvironment
from horizon.environment_context import (
    clear_active_environment,
    set_active_environment,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env_root(tmp_path: Path) -> Iterator[Path]:
    working_dir = tmp_path / "ws"
    working_dir.mkdir()
    set_active_environment(LocalEnvironment(working_dir=working_dir))
    prev = os.getcwd()
    os.chdir(working_dir)
    try:
        yield working_dir
    finally:
        os.chdir(prev)
        clear_active_environment()


def _ctx(state: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=state if state is not None else {},
        tool_confirmation=None,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=lambda **_: None,
    )


class TestForegroundUnchanged:
    async def test_simple_echo_returns_stdout(self, env_root: Path) -> None:
        from horizon.tools.processes.terminal import bash

        result = await bash(command="echo hi", tool_context=_ctx())
        assert result["exit_code"] == 0
        assert "hi" in result["stdout"]
        assert "session_id" not in result  # plain foreground = no session

    async def test_default_path_spills_overflow_to_file(
        self, env_root: Path
    ) -> None:
        # A completed foreground command must spill oversized output to
        # lha/tool-output/ and return a pointer — not silently drop it
        # behind make_preview.
        from horizon.tools.processes.terminal import bash

        result = await bash(
            command=(
                'python3 -c "import sys; '
                "[print('filler-%06d' % i) for i in range(30000)]; "
                "print('TAIL_MARKER_xyz')\""
            ),
            tool_context=_ctx(),
        )
        assert result["truncated"] is True
        assert "session_id" not in result  # completed foreground, not promoted
        overflow_path = result.get("stdout_overflow_path")
        assert overflow_path, f"expected a spill pointer, got {result!r}"
        spilled = Path(overflow_path)
        assert spilled.parent == (env_root / "lha" / "tool-output")
        assert "TAIL_MARKER_xyz" in spilled.read_text()

    async def test_no_cwd_parameter_exists(self, env_root: Path) -> None:
        """Minimal parameter space: no cwd. Use `cd dir && cmd`."""
        import inspect

        from horizon.tools.processes.terminal import bash

        params = inspect.signature(bash).parameters
        assert "cwd" not in params
        assert "background" not in params
        assert "on_timeout" not in params

    async def test_cd_and_cmd_reaches_another_directory(
        self, env_root: Path
    ) -> None:
        from horizon.tools.processes.terminal import bash

        (env_root / "sub").mkdir()
        (env_root / "sub" / "marker.txt").write_text("here")

        result = await bash(
            command="cd sub && cat marker.txt", tool_context=_ctx()
        )

        assert result["exit_code"] == 0
        assert "here" in result["stdout"]


class TestAutoPromote:
    async def test_promote_on_timeout(self, env_root: Path) -> None:
        from horizon.tools.processes.terminal import bash

        ctx = _ctx()
        result = await bash(
            command="echo partial_marker; sleep 30",
            timeout_s=1,
            tool_context=ctx,
        )
        assert result["timed_out"] is True
        assert result["status"] == "running"
        assert result["session_id"].startswith("proc_")
        assert "partial_marker" in result["partial_output"]

    async def test_promoted_session_is_alive(self, env_root: Path) -> None:
        from horizon.tools.processes.process import process
        from horizon.tools.processes.terminal import bash

        ctx = _ctx()
        result = await bash(
            command="sleep 30",
            timeout_s=1,
            tool_context=ctx,
        )
        poll = await process(
            action="poll", session_id=result["session_id"], tool_context=ctx
        )
        assert poll["status"] == "running"

    async def test_promoted_session_lands_in_the_registry(
        self, env_root: Path
    ) -> None:
        from horizon.tools.processes.process import process
        from horizon.tools.processes.terminal import bash

        ctx = _ctx()
        spawn = await bash(command="sleep 30", timeout_s=1, tool_context=ctx)
        listing = await process(action="list", tool_context=ctx)
        running_ids = [s["session_id"] for s in listing["running"]]
        assert spawn["session_id"] in running_ids


class TestNoInBodyGating:
    async def test_does_not_self_gate(self, env_root: Path) -> None:
        """The wrapper does not gate in-body — a confirm-tier policy does
        not short-circuit it. Per-operation approval is the central
        permission_guard's job (before the tool ever runs)."""
        from horizon.tools.processes.terminal import bash

        overlay = env_root / ".lha" / "policies.jsonl"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            # Deliberately the pre-rename name: a real user's existing overlay
            # file must keep working via load-time aliasing.
            '{"canonical_tool_name": "terminal", "requires_confirmation": '
            '{"command": ["dangerous"]}}\n',
            encoding="utf-8",
        )

        ctx = _ctx()
        ctx.tool_confirmation = None
        result = await bash(command="echo dangerous-but-fast", tool_context=ctx)
        assert "confirmation_required" not in result
        assert result["exit_code"] == 0
