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

"""Tests for ``LocalProcessHandle`` — PTY-backed background subprocess.

The handle owns a child process + a daemon reader thread draining a
pseudo-terminal master fd into a rolling byte buffer. Tests cover spawn,
read/offset semantics, interactive stdin, kill, wait, and buffer cap.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest


@pytest.fixture
def handle_factory(tmp_path: Path):
    """Spawn handles in a fresh tmp cwd; tear down any still running."""
    from app.environment.local_process import LocalProcessHandle

    spawned: list[LocalProcessHandle] = []

    def _make(
        command: str,
        *,
        buffer_bytes: int | None = None,
        cwd: Path | None = None,
    ) -> LocalProcessHandle:
        kwargs: dict = {"cwd": cwd or tmp_path}
        if buffer_bytes is not None:
            kwargs["buffer_bytes"] = buffer_bytes
        h = LocalProcessHandle(command, **kwargs)
        spawned.append(h)
        return h

    yield _make

    for h in spawned:
        if h.is_running:
            try:
                asyncio.get_event_loop().run_until_complete(h.kill())
            except RuntimeError:
                # No running loop — best-effort sync kill.
                import os
                import signal

                try:
                    os.killpg(os.getpgid(h.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass


class TestSpawn:
    @pytest.mark.asyncio
    async def test_session_id_format(self, handle_factory) -> None:
        h = handle_factory("true")
        assert h.session_id.startswith("proc_")
        assert len(h.session_id) > len("proc_")

    @pytest.mark.asyncio
    async def test_pid_is_real(self, handle_factory) -> None:
        h = handle_factory("sleep 5")
        assert h.pid > 0

    @pytest.mark.asyncio
    async def test_unique_session_ids(self, handle_factory) -> None:
        h1 = handle_factory("true")
        h2 = handle_factory("true")
        assert h1.session_id != h2.session_id


class TestExitAndWait:
    @pytest.mark.asyncio
    async def test_quick_command_completes(self, handle_factory) -> None:
        h = handle_factory("true")
        exit_code = await h.wait(timeout=5.0)
        assert exit_code == 0
        assert h.is_running is False

    @pytest.mark.asyncio
    async def test_nonzero_exit_preserved(self, handle_factory) -> None:
        h = handle_factory("false")
        exit_code = await h.wait(timeout=5.0)
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_wait_timeout_returns_none_while_running(
        self, handle_factory
    ) -> None:
        h = handle_factory("sleep 5")
        exit_code = await h.wait(timeout=0.2)
        assert exit_code is None
        assert h.is_running is True

    @pytest.mark.asyncio
    async def test_wait_no_timeout_blocks_until_exit(
        self, handle_factory
    ) -> None:
        h = handle_factory("sleep 0.3")
        start = time.monotonic()
        exit_code = await h.wait()
        elapsed = time.monotonic() - start
        assert exit_code == 0
        assert 0.2 < elapsed < 3.0


class TestReadOutput:
    @pytest.mark.asyncio
    async def test_read_captures_stdout(self, handle_factory) -> None:
        h = handle_factory("echo hello_world")
        await h.wait(timeout=5.0)
        data, next_offset, exit_code = await h.read()
        assert b"hello_world" in data
        assert exit_code == 0
        assert next_offset == len(data)

    @pytest.mark.asyncio
    async def test_read_captures_stderr(self, handle_factory) -> None:
        # PTY merges stderr into stdout (single tty); the bytes still show up.
        h = handle_factory("echo to_stderr 1>&2")
        await h.wait(timeout=5.0)
        data, _, _ = await h.read()
        assert b"to_stderr" in data

    @pytest.mark.asyncio
    async def test_read_with_offset_skips_consumed_bytes(
        self, handle_factory
    ) -> None:
        h = handle_factory("echo abcdefghij")
        await h.wait(timeout=5.0)
        first, off1, _ = await h.read(limit=4)
        assert len(first) == 4
        rest, off2, _ = await h.read(offset=off1)
        assert off2 > off1
        # Concatenated round trip yields the full payload.
        full, _, _ = await h.read()
        assert first + rest == full

    @pytest.mark.asyncio
    async def test_partial_read_while_running(self, handle_factory) -> None:
        h = handle_factory("echo first; sleep 0.5; echo second")
        # Give first echo time to land.
        await asyncio.sleep(0.2)
        data, offset, exit_code = await h.read()
        assert b"first" in data
        assert exit_code is None  # still running
        await h.wait(timeout=5.0)
        rest, _, exit_code = await h.read(offset=offset)
        assert b"second" in rest
        assert exit_code == 0


class TestBufferCap:
    @pytest.mark.asyncio
    async def test_rolling_buffer_truncates(self, handle_factory) -> None:
        # Cap at 4KB; emit 16KB. Newest data wins.
        h = handle_factory("yes A | head -c 16384", buffer_bytes=4096)
        await h.wait(timeout=10.0)
        data, _, _ = await h.read()
        # Buffer can't exceed cap. (PTY may add CRs; allow small headroom.)
        assert len(data) <= 4096 + 64

    @pytest.mark.asyncio
    async def test_total_bytes_tracks_full_stream(self, handle_factory) -> None:
        h = handle_factory("yes A | head -c 8192", buffer_bytes=4096)
        await h.wait(timeout=10.0)
        # output_size counts ever-written bytes, not current buffer size.
        assert h.output_size >= 8192


class TestKill:
    @pytest.mark.asyncio
    async def test_kill_stops_running_process(self, handle_factory) -> None:
        h = handle_factory("sleep 30")
        await asyncio.sleep(0.2)
        assert h.is_running is True
        await h.kill()
        assert h.is_running is False
        # Killed processes get a nonzero / signal exit code.
        assert h.exit_code != 0

    @pytest.mark.asyncio
    async def test_kill_on_already_exited_is_noop(self, handle_factory) -> None:
        h = handle_factory("true")
        await h.wait(timeout=5.0)
        await h.kill()  # must not raise

    @pytest.mark.asyncio
    async def test_kill_takes_down_child_tree(self, handle_factory) -> None:
        """A killed handle must take its whole process group down — otherwise
        a `bash -c 'sleep 30'` leaves the sleep orphaned."""
        h = handle_factory("bash -c 'sleep 30 & wait'")
        await asyncio.sleep(0.2)
        await h.kill()
        # Wait a moment for OS reap, then confirm pgid is gone.
        await asyncio.sleep(0.3)
        import os

        with pytest.raises(ProcessLookupError):
            os.killpg(os.getpgid(h.pid), 0)


class TestInteractiveStdin:
    @pytest.mark.asyncio
    async def test_write_drives_a_read_loop(self, handle_factory) -> None:
        """The headline interactive use case: spawn a process that's waiting
        on stdin, write to it, observe the response in stdout."""
        h = handle_factory("bash -c 'read x; echo got=$x'")
        # Let the read block.
        await asyncio.sleep(0.2)
        await h.write(b"hello\n")
        await h.wait(timeout=5.0)
        data, _, _ = await h.read()
        assert b"got=hello" in data

    @pytest.mark.asyncio
    async def test_write_to_exited_process_raises(self, handle_factory) -> None:
        h = handle_factory("true")
        await h.wait(timeout=5.0)
        with pytest.raises(RuntimeError):
            await h.write(b"data\n")


class TestCwd:
    @pytest.mark.asyncio
    async def test_cwd_inherits(self, tmp_path: Path, handle_factory) -> None:
        h = handle_factory("pwd", cwd=tmp_path)
        await h.wait(timeout=5.0)
        data, _, _ = await h.read()
        # PTY adds \r\n; strip CR for comparison.
        produced = Path(data.decode().strip().replace("\r", "")).resolve()
        assert produced == tmp_path.resolve()
