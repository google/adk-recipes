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

"""``SandboxProcessHandle`` — the sandbox arm of the background-process
subsystem. Speaks the ``/processes/*`` REST surface exposed by the
container shim in ``horizon/sandbox/runtime/server.py``.

Mirrors the surface of ``LocalProcessHandle`` so the ``process`` /
``bash`` extension tools can hold either kind in their
``ProcessRegistry``. The two diverge on one point: kill returns the
exit code as reported by the container (the local handle reads it from
``subprocess.Popen.returncode``).
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import httpx


class SandboxProcessHandle:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        session_id: str,
        pid: int,
        command: str,
    ) -> None:
        self._http = http
        self.session_id = session_id
        self.pid = pid
        self.command = command
        self.started_at = time.time()
        self.finished_at: float | None = None

        self._exit_code: int | None = None
        self._output_size = 0
        self._last_status_at = 0.0
        self._last_idle_seconds = 0.0
        self._is_running = True

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def output_size(self) -> int:
        return self._output_size

    @property
    def idle_seconds(self) -> float:
        # Use the value from the last status call, plus the time elapsed
        # since that call — best-effort given the network boundary.
        return self._last_idle_seconds + max(
            0.0, time.time() - self._last_status_at
        )

    async def refresh_status(self) -> dict[str, Any]:
        r = await self._http.get(f"/processes/{self.session_id}/status")
        r.raise_for_status()
        body = r.json()
        self._apply_status(body)
        return body

    def _apply_status(self, body: dict[str, Any]) -> None:
        self._is_running = bool(body.get("is_running", False))
        self._exit_code = body.get("exit_code")
        self._output_size = int(body.get("output_size", 0))
        self._last_idle_seconds = float(body.get("idle_seconds", 0.0))
        self._last_status_at = time.time()
        if not self._is_running and self.finished_at is None:
            self.finished_at = time.time()

    async def read(
        self, offset: int = 0, limit: int | None = None
    ) -> tuple[bytes, int, int | None]:
        params: dict[str, Any] = {"offset": offset}
        if limit is not None:
            params["limit"] = limit
        r = await self._http.get(
            f"/processes/{self.session_id}/output", params=params
        )
        r.raise_for_status()
        body = r.json()
        data = base64.b64decode(body["data_b64"])
        next_offset = int(body["next_offset"])
        exit_code = body.get("exit_code")
        self._exit_code = exit_code
        if exit_code is not None:
            self._is_running = False
            if self.finished_at is None:
                self.finished_at = time.time()
        return data, next_offset, exit_code

    async def write(self, data: bytes) -> None:
        if not self._is_running:
            raise RuntimeError(f"process {self.session_id} has already exited")
        payload = {
            "data_b64": base64.b64encode(data).decode("ascii"),
            "submit": False,
        }
        r = await self._http.post(
            f"/processes/{self.session_id}/stdin", json=payload
        )
        if r.status_code == 409:
            self._is_running = False
            raise RuntimeError(f"process {self.session_id} has already exited")
        r.raise_for_status()

    async def kill(self) -> None:
        if not self._is_running:
            return
        r = await self._http.post(f"/processes/{self.session_id}/kill")
        r.raise_for_status()
        body = r.json()
        self._exit_code = int(body.get("exit_code", -1))
        self._is_running = False
        if self.finished_at is None:
            self.finished_at = time.time()

    async def wait(self, timeout: float | None = None) -> int | None:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        delay = 0.1
        while True:
            await self.refresh_status()
            if not self._is_running:
                return self._exit_code
            if deadline is not None and time.monotonic() >= deadline:
                return None
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)
