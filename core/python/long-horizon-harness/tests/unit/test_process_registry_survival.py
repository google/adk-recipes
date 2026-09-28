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

from __future__ import annotations

import types

from app.environment.registry import (
    DEFAULT_MAX_SESSION_REGISTRIES,
    ProcessRegistry,
    reset_session_registries,
    session_key_from_context,
)


class FakeHandle:
    def __init__(self, session_id: str, *, is_running: bool = True) -> None:
        self.session_id = session_id
        self.pid = 1234
        self.command = "sleep 1000"
        self.started_at = 0.0
        self.finished_at: float | None = None
        self._is_running = is_running

    @property
    def is_running(self) -> bool:
        return self._is_running


def _ctx(session_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _invocation_context=types.SimpleNamespace(
            session=types.SimpleNamespace(id=session_id)
        ),
        state={},
    )


def test_registry_persists_across_turns_by_session_key() -> None:
    reset_session_registries()

    reg1 = ProcessRegistry.for_session("sess-abc")
    reg1.register(FakeHandle("proc_1"))

    # New turn: temp: state would be wiped, but a module-level store keyed
    # by session id must hand back the same registry.
    reg2 = ProcessRegistry.for_session("sess-abc")
    assert reg2 is reg1
    assert reg2.get("proc_1") is not None

    assert ProcessRegistry.for_session("sess-other").get("proc_1") is None


def test_survival_through_real_context_path_across_turns() -> None:
    reset_session_registries()

    # Turn 1: a fresh context resolves to a session key and registers a handle.
    ctx1 = _ctx("sess-1")
    ProcessRegistry.for_session(session_key_from_context(ctx1)).register(
        FakeHandle("proc_1")
    )

    # Turn 2: a brand-new context object with the SAME session id must reach
    # the same registry — proves session_key_from_context + the module store
    # survive the turn boundary together.
    ctx2 = _ctx("sess-1")
    found = ProcessRegistry.for_session(session_key_from_context(ctx2)).get(
        "proc_1"
    )
    assert found is not None

    other = _ctx("sess-2")
    assert (
        ProcessRegistry.for_session(session_key_from_context(other)).get(
            "proc_1"
        )
        is None
    )


def test_cap_evicts_finished_session_registry_when_over_cap() -> None:
    reset_session_registries()

    # Fill exactly to the cap with finished registries.
    for i in range(DEFAULT_MAX_SESSION_REGISTRIES):
        reg = ProcessRegistry.for_session(f"finished-{i}")
        reg.register(FakeHandle(f"proc-{i}", is_running=False))

    # One more over the cap evicts the oldest all-finished registry.
    ProcessRegistry.for_session("overflow")
    assert ProcessRegistry.for_session("overflow") is not None
    # The oldest finished registry (finished-0) was evicted, so re-requesting
    # it yields a fresh empty registry rather than the one holding proc-0.
    assert ProcessRegistry.for_session("finished-0").get("proc-0") is None


def test_cap_keeps_running_session_registry_even_when_over_cap() -> None:
    reset_session_registries()

    # Oldest registry has a RUNNING handle; the rest are finished.
    running = ProcessRegistry.for_session("running-old")
    running.register(FakeHandle("proc-running", is_running=True))
    for i in range(DEFAULT_MAX_SESSION_REGISTRIES - 1):
        ProcessRegistry.for_session(f"finished-{i}").register(
            FakeHandle(f"proc-{i}", is_running=False)
        )

    # Going over the cap must NOT drop the live process behind the model's back.
    ProcessRegistry.for_session("overflow")
    assert (
        ProcessRegistry.for_session("running-old").get("proc-running")
        is not None
    )


def test_cap_never_evicts_just_requested_registry() -> None:
    reset_session_registries()
    for i in range(DEFAULT_MAX_SESSION_REGISTRIES):
        ProcessRegistry.for_session(f"run-{i}").register(
            FakeHandle(f"p-{i}", is_running=True)
        )
    reg = ProcessRegistry.for_session("fresh")
    reg.register(FakeHandle("proc_live", is_running=True))
    assert ProcessRegistry.for_session("fresh").get("proc_live") is not None
