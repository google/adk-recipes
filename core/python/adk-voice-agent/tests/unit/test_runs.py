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

"""Unit tests for `app/workers/runs.py` and `sandbox/server.py`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.workers import runs
from app.workers.environment import LocalEnvironment, SandboxEnvironment
from sandbox import server as daemon_server
from tests.fakes import wait_until

pytestmark = pytest.mark.asyncio


async def test_runs_poll_reads_complete_lines_and_skips_oversized_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "POLL_READ_CAP", 64)
    env = LocalEnvironment(tmp_path)
    script = (
        "import sys, time\n"
        "print('first', flush=True)\n"
        "sys.stdout.write('x' * 200 + '\\n'); sys.stdout.flush()\n"
        "sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(0.5)\n"
        "print(' done', flush=True)\n"
    )
    handle = await runs.launch(
        env, run_id="r1", argv=["python3", "-c", script], cwd=str(tmp_path)
    )
    cursor = runs.RunCursor()
    lines: list[str] = []
    exit_codes: list[int] = []

    async def drained() -> bool:
        polled = await runs.poll(env, handle, cursor)
        lines.extend(polled.lines)
        if polled.exit_code is not None and polled.bytes_read == 0:
            exit_codes.append(polled.exit_code)
        return bool(exit_codes)

    await wait_until(
        drained, timeout_s=10, interval_s=0.05, message="the run to exit"
    )
    assert exit_codes == [0]
    assert lines == ["first", "partial done"]


async def test_runs_cancel_and_lost_detection(tmp_path: Path) -> None:
    env = LocalEnvironment(tmp_path)
    handle = await runs.launch(
        env, run_id="r2", argv=["sleep", "30"], cwd=str(tmp_path)
    )
    assert (await runs.poll(env, handle, runs.RunCursor())).exit_code is None
    await runs.cancel(env, handle)
    assert (await runs.poll(env, handle, runs.RunCursor())).exit_code == -15

    missing = await runs.poll(
        env, runs.RunHandle("gone", str(tmp_path / "gone")), runs.RunCursor()
    )
    assert missing.lost and missing.error and "missing" in missing.error

    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / "pid").write_text("999999")
    assert (
        await runs.poll(
            env, runs.RunHandle("orphan", str(orphan)), runs.RunCursor()
        )
    ).lost


async def test_a_run_past_its_wall_clock_limit_is_killed(
    tmp_path: Path,
) -> None:
    env = LocalEnvironment(tmp_path)
    handle = await runs.launch(
        env,
        run_id="r-limit",
        argv=["sleep", "30"],
        cwd=str(tmp_path),
        max_runtime_s=0.5,
    )
    polled: list[runs.RunPoll] = []

    async def finished() -> bool:
        polled.append(await runs.poll(env, handle, runs.RunCursor()))
        return polled[-1].exit_code is not None

    await wait_until(
        finished, timeout_s=10, interval_s=0.1, message="the time limit"
    )
    assert polled[-1].exit_code == -9
    assert polled[-1].stderr_tail.startswith("Stopped: the run exceeded")
    spec = json.loads(
        (tmp_path / ".voice-agent/runs/r-limit/spec.json").read_text()
    )
    assert spec["max_runtime_s"] == 0.5


async def test_runs_get_a_default_limit_from_the_run_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(tmp_path))
    (tmp_path / "policy.yaml").write_text(
        "run_budget:\n  stall_timeout_min: 2\n"
    )
    assert runs.default_max_runtime_s() == 2 * 60 + runs.RUN_GRACE_S


def _daemon_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SandboxEnvironment:
    monkeypatch.setattr(daemon_server, "WORKSPACE", str(tmp_path))
    daemon_server._active_runs.clear()
    return SandboxEnvironment(
        name="cloud-computer-test",
        url="https://adk-voice-agent-worker.run.app",
        access_token="id-token",
        workspace_root=str(tmp_path),
        transport=httpx.ASGITransport(app=daemon_server.app),
    )


async def test_cloud_computer_daemon_direct_runs_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _daemon_env(tmp_path, monkeypatch)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'echo \'{"type":"system","subtype":"init","session_id":"s-1"}\'\n'
        "sleep 0.3\n"
        'echo \'{"type":"result","subtype":"success","result":"ok"}\'\n'
    )
    fake_claude.chmod(0o755)

    handle = await runs.launch(
        env,
        run_id="daemon-r1",
        argv=[str(fake_claude), "-p", "hi"],
        cwd=str(tmp_path),
    )
    assert handle.run_dir == f"{tmp_path}/.voice-agent/runs/daemon-r1"

    cursor = runs.RunCursor()
    collected: list[str] = []
    exit_codes: list[int] = []

    async def finished() -> bool:
        polled = await runs.poll(env, handle, cursor)
        collected.extend(polled.lines)
        if polled.exit_code is not None and polled.bytes_read == 0:
            exit_codes.append(polled.exit_code)
            return True
        return False

    await wait_until(
        finished, timeout_s=10, interval_s=0.05, message="daemon run to finish"
    )
    assert exit_codes == [0]
    assert len(collected) == 2

    slow_handle = await runs.launch(
        env,
        run_id="daemon-cancel",
        argv=["sleep", "30"],
        cwd=str(tmp_path),
    )
    await runs.cancel(env, slow_handle)
    polled = await runs.poll(env, slow_handle, runs.RunCursor())
    assert polled.exit_code == -15
