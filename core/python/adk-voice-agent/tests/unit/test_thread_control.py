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

"""Thread control: cancelling, redirecting, deleting a thread, and rejecting actions in the wrong state."""

from __future__ import annotations

import asyncio

import pytest

from app.auth.identity import run_as
from app.threads import registry as registry_module
from app.threads.model import EventKind, ThreadState
from app.threads.registry import ThreadError
from tests.fakes import GOAL, REPO, Lab, kinds, wait_until

pytestmark = pytest.mark.asyncio


async def test_cancel_stops_the_run_and_the_thread_stays_steerable(lab: Lab):
    lab.agent.script({"steps": [{"say": "thinking"}, {"hang": True}]}, {})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.wait_for_calls(1)

    await lab.registry.cancel(handle.thread_id)
    assert (
        handle.state is ThreadState.CANCELLED
        and kinds(handle)[-1] == "cancelled"
    )
    assert lab.exit_code(f"{handle.thread_id}-1-1") != 0
    with pytest.raises(ThreadError):
        await lab.registry.cancel(handle.thread_id)

    await lab.registry.steer(handle.thread_id, "Try again, smaller scope")
    handle = await lab.settle(handle.thread_id)
    assert handle.iteration == 2 and handle.state is ThreadState.COMPLETED


async def test_cancel_during_launch_still_stops_the_process(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
):
    launched, release = asyncio.Event(), asyncio.Event()
    real_exec = lab.env.exec

    async def slow_launch(command: str, **kwargs):
        result = await real_exec(command, **kwargs)
        if "runner.py" in command:
            # The process now exists, but `start()` hasn't returned yet.
            launched.set()
            await release.wait()
        return result

    monkeypatch.setattr(lab.env, "exec", slow_launch)
    lab.agent.script({"steps": [{"hang": True}]})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await asyncio.wait_for(launched.wait(), 10)

    cancelling = asyncio.create_task(lab.registry.cancel(handle.thread_id))
    # The registry detaches the driver right before cancelling it.
    await wait_until(lambda: handle.driver is None, message="cancel to start")
    release.set()
    await asyncio.wait_for(cancelling, 20)
    assert handle.state is ThreadState.CANCELLED
    assert lab.exit_code(f"{handle.thread_id}-1-1") != 0


async def test_steering_a_running_thread_redirects_it(lab: Lab):
    lab.agent.script({"steps": [{"say": "working"}, {"hang": True}]}, {})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.wait_for_calls(1)

    await lab.registry.steer(handle.thread_id, "Name it hello() instead")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED
    redirect = lab.agent.calls[1]
    assert "Name it hello() instead" in redirect["prompt"]


async def test_steering_waits_for_the_previous_run_to_stop(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
):
    lab.agent.script({"steps": [{"hang": True}]})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.wait_for_calls(1)

    async def ignores_one_cancel() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)

    real_driver = handle.driver
    stuck = asyncio.create_task(ignores_one_cancel())
    await asyncio.sleep(0)
    handle.driver = stuck
    monkeypatch.setattr(registry_module, "_STOP_TIMEOUT_S", 0.05)

    with pytest.raises(ThreadError, match="Still stopping the previous run"):
        await lab.registry.steer(handle.thread_id, "Smaller scope")
    assert handle.driver is stuck, "no second driver was started"
    assert len(lab.agent.calls) == 1

    stuck.cancel()
    handle.driver = real_driver
    monkeypatch.undo()
    await lab.registry.cancel(handle.thread_id)


async def test_approving_right_as_the_permission_lands_keeps_events_in_order(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
):
    """An approval between the gate's state change and its announcement waits for the announcement."""
    lab.agent.script({"steps": [{"run": "git push origin main"}]}, {})
    emit = lab.registry.emit

    async def slow_permission_requested(handle, kind, *args, **kwargs):
        if kind is EventKind.PERMISSION_REQUESTED:
            await asyncio.sleep(0.2)
        return await emit(handle, kind, *args, **kwargs)

    monkeypatch.setattr(lab.registry, "emit", slow_permission_requested)
    handle = await lab.registry.dispatch(GOAL, REPO)
    await wait_until(lambda: handle.state is ThreadState.AWAITING_PERMISSION)
    await lab.registry.approve(handle.thread_id)
    handle = await lab.settle(handle.thread_id)

    assert handle.state is ThreadState.COMPLETED
    order = [
        e.kind
        if e.kind is not EventKind.STATE_CHANGED
        else f"-> {e.data['to']}"
        for e in handle.events
    ]
    perm_idx = order.index(EventKind.PERMISSION_REQUESTED)
    resumed_idx = order.index("-> executing", perm_idx)
    assert perm_idx < resumed_idx


async def test_gate_actions_are_rejected_in_the_wrong_state(lab: Lab):
    lab.agent.script({})
    handle = await lab.dispatch()
    with pytest.raises(ThreadError, match="nothing waiting for approval"):
        await lab.registry.approve(handle.thread_id)
    with pytest.raises(ThreadError, match="No thread"):
        await lab.registry.steer("thread-missing", "hello")
    with pytest.raises(ThreadError):
        await lab.registry.dispatch("  ", REPO)
    with pytest.raises(ThreadError, match="Unknown coding harness"):
        await lab.registry.dispatch(GOAL, REPO, harness="nope")
    with pytest.raises(
        ThreadError, match=f"no repository named 'nope'.*{REPO}"
    ):
        await lab.registry.dispatch(GOAL, "code/nope")


# ---- thread deletion --------------------------------------------------------


async def test_deleting_a_thread_removes_it_and_its_history(lab: Lab):
    keep = await lab.dispatch()
    gone = await lab.dispatch()
    assert gone.state is ThreadState.COMPLETED

    await lab.registry.delete(gone.thread_id)

    assert await lab.registry.get(gone.thread_id) is None
    assert [t.thread_id for t in await lab.registry.list_all()] == [
        keep.thread_id
    ]
    # Nothing left in the store either: a restart doesn't bring it back.
    lab.registry._threads.clear()
    assert await lab.registry.get(gone.thread_id) is None
    assert await lab.registry.get(keep.thread_id) is not None


async def test_a_running_thread_is_stopped_before_it_is_deleted(lab: Lab):
    lab.agent.script({"steps": [{"hang": True}]})
    handle = await lab.registry.dispatch("Slow work", "voice-agent-fixture")
    await lab.wait_for_calls(1)
    assert handle.state is ThreadState.EXECUTING

    await lab.registry.delete(handle.thread_id)

    assert handle.driver is None
    assert await lab.registry.get(handle.thread_id) is None


async def test_no_one_deletes_another_users_thread(lab: Lab):
    handle = await lab.dispatch()
    with run_as("someone-else"), pytest.raises(ThreadError):
        await lab.registry.delete(handle.thread_id)
    assert await lab.registry.get(handle.thread_id) is not None
