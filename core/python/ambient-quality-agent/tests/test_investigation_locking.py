# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the claim that keeps one observed agent to one sweep at a time."""

from __future__ import annotations

import threading

from ambient_quality_agent.core.investigation import locking

from .conftest import make_config


def test_the_first_claim_on_an_agent_is_granted() -> None:
    lock = locking.InProcessInvestigationLock()
    assert lock.acquire("agent-a", "run-1") is None


def test_a_second_claim_names_the_run_already_holding_it() -> None:
    # The id is the whole point: the caller answers with that run rather than
    # starting a second sweep.
    lock = locking.InProcessInvestigationLock()
    lock.acquire("agent-a", "run-1")
    assert lock.acquire("agent-a", "run-2") == "run-1"


def test_a_released_claim_is_grantable_again() -> None:
    lock = locking.InProcessInvestigationLock()
    lock.acquire("agent-a", "run-1")
    lock.release("agent-a", "run-1")
    assert lock.acquire("agent-a", "run-2") is None


def test_a_release_by_anyone_but_the_holder_does_nothing() -> None:
    # The refused run releases in its own `finally` too, and must not hand the
    # agent away from the sweep that is still running.
    lock = locking.InProcessInvestigationLock()
    lock.acquire("agent-a", "run-1")
    lock.release("agent-a", "run-2")
    assert lock.acquire("agent-a", "run-3") == "run-1"


def test_two_agents_do_not_block_each_other() -> None:
    lock = locking.InProcessInvestigationLock()
    lock.acquire("agent-a", "run-1")
    assert lock.acquire("agent-b", "run-2") is None


def test_only_one_of_many_threads_gets_the_claim() -> None:
    lock = locking.InProcessInvestigationLock()
    start = threading.Barrier(8)
    granted: list[str] = []
    guard = threading.Lock()

    def claim(run_id: str) -> None:
        start.wait(timeout=5)
        if lock.acquire("agent-a", run_id) is None:
            with guard:
                granted.append(run_id)

    threads = [
        threading.Thread(target=claim, args=(f"run-{i}",)) for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(granted) == 1


def test_the_unguarded_lock_grants_every_claim() -> None:
    # In durable execution, the sweep runs as an independent Agent Runtime
    # execution that cannot be observed from within this process.
    lock = locking.UnguardedInvestigationLock()
    assert lock.acquire("agent-a", "run-1") is None
    assert lock.acquire("agent-a", "run-2") is None


def test_the_default_lock_follows_where_the_sweep_runs() -> None:
    inline = locking.lock_factory(make_config(sync_investigation=True))
    durable = locking.lock_factory(make_config(sync_investigation=False))
    assert isinstance(inline, locking.InProcessInvestigationLock)
    assert isinstance(durable, locking.UnguardedInvestigationLock)


def test_the_in_process_lock_is_one_per_process() -> None:
    # Two instances would each grant the same claim, so the factory must hand
    # out the same one to every launch.
    config = make_config(sync_investigation=True)
    assert locking.lock_factory(config) is locking.lock_factory(config)
