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

"""Tests for the ``process`` tool — single-function multi-action dispatcher.

Actions: list, poll, log, wait, kill, write. The model interacts with
backgrounded sessions exclusively through this tool. State is read from
a passed-in tool_context.state dict (the registry lives there).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def context_factory(tmp_path: Path):
    """Build a minimal SimpleNamespace tool_context with a fresh state dict."""

    def _make() -> SimpleNamespace:
        return SimpleNamespace(state={})

    return _make


@pytest.fixture
def spawn_into(tmp_path: Path):
    """Register a fresh background handle into the given context's registry."""
    from horizon.environment.local_process import LocalProcessHandle
    from horizon.environment.registry import ProcessRegistry

    spawned: list[LocalProcessHandle] = []

    def _spawn(ctx, command: str = "true") -> LocalProcessHandle:
        h = LocalProcessHandle(command, cwd=tmp_path)
        spawned.append(h)
        ProcessRegistry.for_state(ctx.state).register(h)
        return h

    yield _spawn

    for h in spawned:
        if h.is_running:
            import os
            import signal

            try:
                os.killpg(os.getpgid(h.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
class TestListAction:
    async def test_list_empty(self, context_factory) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(action="list", tool_context=ctx)
        assert result == {"running": [], "finished": []}

    async def test_list_partitions_by_state(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        live = spawn_into(ctx, "sleep 5")
        done = spawn_into(ctx, "true")
        await done.wait(timeout=5.0)

        result = await process(action="list", tool_context=ctx)
        running_ids = [s["session_id"] for s in result["running"]]
        finished_ids = [s["session_id"] for s in result["finished"]]
        assert live.session_id in running_ids
        assert done.session_id in finished_ids


@pytest.mark.asyncio
class TestPollAction:
    async def test_poll_running_returns_status(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "echo hi; sleep 5")
        await asyncio.sleep(0.2)
        result = await process(
            action="poll", session_id=h.session_id, tool_context=ctx
        )
        assert result["status"] == "running"
        assert result["exit_code"] is None
        assert "hi" in result["tail"]

    async def test_poll_exited_returns_exit_code(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "echo done")
        await h.wait(timeout=5.0)
        result = await process(
            action="poll", session_id=h.session_id, tool_context=ctx
        )
        assert result["status"] == "exited"
        assert result["exit_code"] == 0

    async def test_poll_unknown_session_returns_error(
        self, context_factory
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(
            action="poll", session_id="proc_unknown", tool_context=ctx
        )
        assert "error" in result


@pytest.mark.asyncio
class TestLogAction:
    async def test_log_returns_paginated_chunk(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "yes A | head -c 200")
        await h.wait(timeout=5.0)
        first = await process(
            action="log",
            session_id=h.session_id,
            offset=0,
            limit=50,
            tool_context=ctx,
        )
        assert len(first["data"]) <= 50
        assert first["next_offset"] > 0
        second = await process(
            action="log",
            session_id=h.session_id,
            offset=first["next_offset"],
            tool_context=ctx,
        )
        assert second["next_offset"] >= first["next_offset"]


@pytest.mark.asyncio
class TestWaitAction:
    async def test_wait_returns_exit_code(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sleep 0.3")
        result = await process(
            action="wait",
            session_id=h.session_id,
            timeout_s=5.0,
            tool_context=ctx,
        )
        assert result["status"] == "exited"
        assert result["exit_code"] == 0

    async def test_wait_timeout_returns_running(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sleep 5")
        result = await process(
            action="wait",
            session_id=h.session_id,
            timeout_s=0.2,
            tool_context=ctx,
        )
        assert result["status"] == "running"
        assert result["exit_code"] is None


@pytest.mark.asyncio
class TestKillAction:
    async def test_kill_stops_process(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sleep 30")
        await asyncio.sleep(0.2)
        result = await process(
            action="kill", session_id=h.session_id, tool_context=ctx
        )
        assert result["status"] == "killed"
        assert h.is_running is False


@pytest.mark.asyncio
class TestWriteAction:
    async def test_write_drives_interactive(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "bash -c 'read x; echo got=$x'")
        await asyncio.sleep(0.2)
        await process(
            action="write",
            session_id=h.session_id,
            data="hi",
            tool_context=ctx,
        )
        await h.wait(timeout=5.0)
        tail, _, _ = await h.read()
        assert b"got=hi" in tail

    async def test_write_to_unknown_session_errors(
        self, context_factory
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(
            action="write",
            session_id="proc_unknown",
            data="x",
            tool_context=ctx,
        )
        assert "error" in result

    async def test_write_oserror_returns_error_envelope(
        self, context_factory
    ) -> None:
        from horizon.environment.registry import ProcessRegistry
        from horizon.tools.processes.process import process

        class _DeadPtyHandle:
            session_id = "proc_dead_pty"
            is_running = True

            async def write(self, data: bytes) -> None:
                raise OSError(5, "Input/output error")

        ctx = context_factory()
        ProcessRegistry.for_state(ctx.state).register(_DeadPtyHandle())

        result = await process(
            action="write",
            session_id="proc_dead_pty",
            data="payload",
            tool_context=ctx,
        )
        assert "error" in result
        assert (
            "write failed" in result["error"].lower()
            or "input/output" in result["error"].lower()
        )


@pytest.mark.asyncio
class TestActionEnum:
    async def test_unknown_action_errors(self, context_factory) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(action="frobnicate", tool_context=ctx)
        assert "error" in result


@pytest.mark.asyncio
class TestWaitForAction:
    async def test_matches_pattern_in_output(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sh -c 'sleep 0.2; echo READY-MARKER; sleep 5'")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            pattern="READY-MARKER",
            timeout_s=5.0,
            tool_context=ctx,
        )
        assert out["matched"] is True
        assert out["match"] == "READY-MARKER"
        assert out["status"] == "running"
        await h.kill()

    async def test_regex_alternation(self, context_factory, spawn_into) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sh -c 'sleep 0.1; echo boom-Error-here; sleep 5'")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            pattern="Listening on|Error",
            timeout_s=5.0,
            tool_context=ctx,
        )
        assert out["matched"] is True
        assert out["match"] == "Error"
        await h.kill()

    async def test_returns_when_process_exits_before_match(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sh -c 'echo nope; exit 0'")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            pattern="NEVER-APPEARS",
            timeout_s=3.0,
            tool_context=ctx,
        )
        assert out["matched"] is False
        assert out["status"] == "exited"
        assert out["timed_out"] is False

    async def test_times_out_while_running(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "sh -c 'sleep 10'")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            pattern="X",
            timeout_s=0.3,
            tool_context=ctx,
        )
        assert out["matched"] is False
        assert out["timed_out"] is True
        assert out["status"] == "running"
        await h.kill()

    async def test_invalid_regex_returns_error(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "true")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            pattern="[unclosed",
            timeout_s=1.0,
            tool_context=ctx,
        )
        assert "error" in out
        await h.kill()

    async def test_missing_pattern_rejected(
        self, context_factory, spawn_into
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        h = spawn_into(ctx, "true")
        out = await process(
            action="wait_for",
            session_id=h.session_id,
            timeout_s=1.0,
            tool_context=ctx,
        )
        assert "error" in out and "pattern" in out["error"]


@pytest.fixture
def spawn_env(tmp_path: Path):
    """Active environment for process(action='spawn'), which — unlike every
    other action — calls active_environment() itself (bash's former
    background=True moved here). Separate from context_factory/spawn_into,
    which construct LocalProcessHandle directly and need no environment."""
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import (
        clear_active_environment,
        set_active_environment,
    )

    working_dir = tmp_path / "ws"
    working_dir.mkdir()
    set_active_environment(LocalEnvironment(working_dir=working_dir))
    try:
        yield working_dir
    finally:
        clear_active_environment()


@pytest.mark.asyncio
class TestSpawnAction:
    """process(action='spawn') is where bash's former background=True moved
    (the minimal parameter space dropped it from bash). The
    guardrail-facing
    behavior (command_safety deny, command-substitution ask) is covered
    behaviorally in test_process_spawn_guardrails.py, not here — this file
    pins the tool's own dispatch and registry mechanics."""

    async def test_spawn_requires_command(self, context_factory) -> None:
        # No active environment needed: process() rejects a commandless
        # spawn before ever calling active_environment().
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(action="spawn", tool_context=ctx)
        assert "error" in result and "command" in result["error"]

    async def test_spawn_cwd_outside_root_is_rejected_before_spawning(
        self, context_factory, spawn_env: Path
    ) -> None:
        # path_under_root runs before open_handle, so an outside-root cwd
        # never spawns a process at all.
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(
            action="spawn",
            command="true",
            cwd="/etc",
            tool_context=ctx,
        )
        assert "error" in result
        assert "session_id" not in result

    async def test_spawn_returns_session_id_immediately(
        self, context_factory, spawn_env: Path
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        result = await process(
            action="spawn", command="sleep 5", tool_context=ctx
        )
        assert result["status"] == "running"
        assert result["session_id"].startswith("proc_")
        assert result["pid"] > 0

    async def test_spawn_handle_lands_in_registry(
        self, context_factory, spawn_env: Path
    ) -> None:
        from horizon.tools.processes.process import process

        ctx = context_factory()
        spawn = await process(
            action="spawn", command="sleep 5", tool_context=ctx
        )
        listing = await process(action="list", tool_context=ctx)
        running_ids = [s["session_id"] for s in listing["running"]]
        assert spawn["session_id"] in running_ids

    async def test_spawn_does_not_block(
        self, context_factory, spawn_env: Path
    ) -> None:
        import time

        from horizon.tools.processes.process import process

        ctx = context_factory()
        start = time.monotonic()
        await process(action="spawn", command="sleep 30", tool_context=ctx)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0

    async def test_spawn_defaults_cwd_to_workspace_root(
        self, context_factory, spawn_env: Path
    ) -> None:
        from horizon.tools.processes.process import process

        (spawn_env / "marker.txt").write_text("here")
        ctx = context_factory()
        spawn = await process(
            action="spawn", command="cat marker.txt", tool_context=ctx
        )
        result = await process(
            action="wait",
            session_id=spawn["session_id"],
            timeout_s=5.0,
            tool_context=ctx,
        )
        assert result["exit_code"] == 0
        log = await process(
            action="log", session_id=spawn["session_id"], tool_context=ctx
        )
        assert "here" in log["data"]
