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

"""``LocalProcessHandle`` — a PTY-backed background subprocess on the host.

Spawns a child under ``/bin/sh -c`` attached to a pseudo-terminal, runs it
in its own process group so the whole tree can be killed, and drains the
master fd in a daemon thread into a rolling byte buffer. The local arm of
the background-process subsystem; ``ProcessRegistry`` and the
``process``/``bash`` tools sit above it. The sandbox arm
(``SandboxProcessHandle``) lives next to it in ``horizon/environment`` and
speaks HTTP to the sandbox shim."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import pty
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

DEFAULT_BUFFER_BYTES = 200 * 1024  # 200 KB rolling cap per session

__all__ = ["DEFAULT_BUFFER_BYTES", "LocalProcessHandle"]


class LocalProcessHandle:
    def __init__(
        self,
        command: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
    ) -> None:
        self.session_id = f"proc_{uuid.uuid4().hex[:12]}"
        self.command = command
        self.started_at = time.time()
        self.finished_at: float | None = None

        self._buffer_bytes = buffer_bytes
        self._buffer = bytearray()
        self._total_bytes = 0
        self._lock = threading.Lock()
        self._exit_code: int | None = None
        self._exit_event = threading.Event()
        self._last_output_at = self.started_at

        self._master_fd, slave_fd = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                start_new_session=True,  # setsid, so kill() can killpg the tree
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        self.pid = self._proc.pid

        flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"lha-proc-reader-{self.session_id}",
            daemon=True,
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self._master_fd, 4096)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        # No data available; if child has exited, drain once
                        # more then stop, else nap.
                        if self._proc.poll() is not None:
                            try:
                                chunk = os.read(self._master_fd, 4096)
                                if not chunk:
                                    break
                            except OSError:
                                break
                        else:
                            time.sleep(0.02)
                            continue
                    elif exc.errno == errno.EIO:
                        # Slave-side closed (child exited and released the PTY).
                        break
                    else:
                        break
                if not chunk:
                    break
                with self._lock:
                    self._buffer.extend(chunk)
                    self._total_bytes += len(chunk)
                    self._last_output_at = time.time()
                    overflow = len(self._buffer) - self._buffer_bytes
                    if overflow > 0:
                        del self._buffer[:overflow]
        finally:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._exit_code = self._proc.returncode
            self.finished_at = time.time()
            self._exit_event.set()
            try:
                os.close(self._master_fd)
            except OSError:
                pass

    @property
    def is_running(self) -> bool:
        return not self._exit_event.is_set()

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def output_size(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_output_at

    async def read(
        self, offset: int = 0, limit: int | None = None
    ) -> tuple[bytes, int, int | None]:
        with self._lock:
            buffer_start = self._total_bytes - len(self._buffer)
            slice_start = max(offset, buffer_start) - buffer_start
            slice_start = max(0, min(slice_start, len(self._buffer)))
            if limit is None:
                data = bytes(self._buffer[slice_start:])
            else:
                data = bytes(self._buffer[slice_start : slice_start + limit])
            next_offset = buffer_start + slice_start + len(data)
            return data, next_offset, self._exit_code

    async def write(self, data: bytes) -> None:
        if self._exit_event.is_set():
            raise RuntimeError(f"process {self.session_id} has already exited")
        await asyncio.to_thread(os.write, self._master_fd, data)

    async def kill(self) -> None:
        if self._exit_event.is_set():
            return
        try:
            pgid = os.getpgid(self.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        exited = await asyncio.to_thread(self._exit_event.wait, 2.0)
        if exited:
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await asyncio.to_thread(self._exit_event.wait, 1.0)

    async def wait(self, timeout: float | None = None) -> int | None:
        await asyncio.to_thread(self._exit_event.wait, timeout)
        return self._exit_code
