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

"""Tests for ``ProcessRegistry`` — session-scoped store of background processes.

Registry caps total sessions (LRU evicts oldest finished), tracks running
vs finished, expires finished sessions after a TTL. State is keyed by
session.state["temp:process_registry"] so each ADK session has its own.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def make_handle(tmp_path: Path):
    from app.environment.local_process import LocalProcessHandle

    spawned: list[LocalProcessHandle] = []

    def _make(command: str = "true") -> LocalProcessHandle:
        h = LocalProcessHandle(command, cwd=tmp_path)
        spawned.append(h)
        return h

    yield _make

    for h in spawned:
        if h.is_running:
            import os
            import signal

            try:
                os.killpg(os.getpgid(h.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


class TestBasics:
    def test_register_and_get(self, make_handle) -> None:
        from app.environment.registry import ProcessRegistry

        reg = ProcessRegistry()
        h = make_handle()
        reg.register(h)
        assert reg.get(h.session_id) is h

    def test_get_unknown_returns_none(self) -> None:
        from app.environment.registry import ProcessRegistry

        assert ProcessRegistry().get("proc_nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_split_by_status(self, make_handle) -> None:
        from app.environment.registry import ProcessRegistry

        reg = ProcessRegistry()
        running = make_handle("sleep 5")
        finished = make_handle("true")
        await finished.wait(timeout=5.0)
        reg.register(running)
        reg.register(finished)

        running_ids = [h.session_id for h in reg.list_running()]
        finished_ids = [h.session_id for h in reg.list_finished()]
        assert running.session_id in running_ids
        assert finished.session_id in finished_ids
        assert finished.session_id not in running_ids
        assert running.session_id not in finished_ids


class TestCap:
    @pytest.mark.asyncio
    async def test_lru_evicts_oldest_finished(self, make_handle) -> None:
        from app.environment.registry import ProcessRegistry

        reg = ProcessRegistry(max_sessions=3)
        old = make_handle("true")
        await old.wait(timeout=5.0)
        reg.register(old)

        # Fill past cap with new finished sessions.
        for _ in range(5):
            h = make_handle("true")
            await h.wait(timeout=5.0)
            reg.register(h)

        # Oldest finished must have been evicted.
        assert reg.get(old.session_id) is None
        all_known = {
            h.session_id for h in reg.list_running() + reg.list_finished()
        }
        assert len(all_known) <= 3

    @pytest.mark.asyncio
    async def test_running_processes_never_evicted(self, make_handle) -> None:
        from app.environment.registry import ProcessRegistry

        reg = ProcessRegistry(max_sessions=2)
        # Two running first (count against the cap).
        r1 = make_handle("sleep 10")
        r2 = make_handle("sleep 10")
        reg.register(r1)
        reg.register(r2)

        # Adding more should NOT evict the running ones; either it raises
        # or it accepts but keeps r1/r2 reachable.
        new_finished = make_handle("true")
        await new_finished.wait(timeout=5.0)
        reg.register(new_finished)

        assert reg.get(r1.session_id) is r1
        assert reg.get(r2.session_id) is r2


class TestTTL:
    @pytest.mark.asyncio
    async def test_finished_sessions_drop_after_ttl(self, make_handle) -> None:
        from app.environment.registry import ProcessRegistry

        reg = ProcessRegistry(finished_ttl_s=0.2)
        h = make_handle("true")
        await h.wait(timeout=5.0)
        reg.register(h)
        assert reg.get(h.session_id) is h

        time.sleep(0.3)
        reg.gc()
        assert reg.get(h.session_id) is None


class TestSessionStateBinding:
    def test_for_state_creates_if_missing(self) -> None:
        from app.environment.registry import (
            REGISTRY_STATE_KEY,
            ProcessRegistry,
        )

        state: dict = {}
        reg = ProcessRegistry.for_state(state)
        assert isinstance(reg, ProcessRegistry)
        assert state[REGISTRY_STATE_KEY] is reg

    def test_for_state_reuses_existing(self) -> None:
        from app.environment.registry import ProcessRegistry

        state: dict = {}
        first = ProcessRegistry.for_state(state)
        second = ProcessRegistry.for_state(state)
        assert first is second
