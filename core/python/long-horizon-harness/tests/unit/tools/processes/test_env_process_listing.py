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

"""BaseEnvironment process-listing interface (Task 1): LocalEnvironment maps the
in-process registry to normalized rows; kill delegates to the handle."""

from __future__ import annotations

import pytest

from app.environment import LocalEnvironment
from app.environment import registry as reg


class _FakeHandle:
    def __init__(self, sid: str, running: bool) -> None:
        self.session_id = sid
        self.pid = 4321
        self.command = f"sleep for {sid}"
        self.started_at = 100.0
        self.finished_at = None if running else 200.0
        self._running = running
        self.killed = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def exit_code(self) -> int | None:
        return None if self._running else 0

    @property
    def output_size(self) -> int:
        return 42

    @property
    def idle_seconds(self) -> float:
        return 1.5

    async def kill(self) -> None:
        self.killed = True
        self._running = False


@pytest.fixture(autouse=True)
def _clean_registry():
    reg.reset_session_registries()
    yield
    reg.reset_session_registries()


@pytest.mark.asyncio
async def test_local_list_processes_maps_registry(tmp_path) -> None:
    h = _FakeHandle("proc_abc", running=True)
    reg.ProcessRegistry.for_session("sess-1").register(h)
    env = LocalEnvironment(working_dir=tmp_path)
    rows = await env.list_processes()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "proc_abc"
    assert rows[0]["running"] is True
    assert rows[0]["command"] == "sleep for proc_abc"
    assert rows[0]["output_size"] == 42


@pytest.mark.asyncio
async def test_local_kill_process_calls_handle(tmp_path) -> None:
    h = _FakeHandle("proc_kill", running=True)
    reg.ProcessRegistry.for_session("sess-2").register(h)
    env = LocalEnvironment(working_dir=tmp_path)
    assert await env.kill_process("proc_kill") is True
    assert h.killed is True
    assert await env.kill_process("proc_missing") is False


@pytest.mark.asyncio
async def test_base_defaults_are_empty(tmp_path) -> None:
    reg.reset_session_registries()
    env = LocalEnvironment(working_dir=tmp_path)
    assert await env.list_processes() == []
