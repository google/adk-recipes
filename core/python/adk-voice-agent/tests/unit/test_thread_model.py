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

"""Thread state machine, shared wording, turns, and exactly-once milestone delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from app.threads import describe
from app.threads.model import (
    FINISHED_STATES,
    TRANSITIONS,
    EventKind,
    InvalidTransition,
    ThreadHandle,
    ThreadState,
    Turn,
)
from app.threads.registry import ThreadRegistry, set_thread_registry
from app.voice.thread_updates import ThreadUpdates


def make_handle(state: ThreadState = ThreadState.EXECUTING) -> ThreadHandle:
    return ThreadHandle(
        thread_id="thread-1",
        goal="g",
        project="code/r",
        harness="claude",
        branch="b",
        state=state,
    )


@pytest.fixture
def registry() -> Iterator[ThreadRegistry]:
    registry = ThreadRegistry(store=None)
    set_thread_registry(registry)
    yield registry
    set_thread_registry(None)


class FakeLiveQueue:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_content(self, content) -> None:  # type: ignore[no-untyped-def]
        self.sent.append(content.parts[0].text)


def test_every_state_has_transitions_and_wording():
    for table in (
        TRANSITIONS,
        describe.STATE_LABELS,
        describe.STATE_PHRASES,
        describe.NEXT_STEPS,
    ):
        assert set(table) == set(ThreadState)


def test_plain_summary_strips_markdown_but_keeps_identifiers():
    text = "**Fixed** `test_retry.py`; set _DATABASE_URL_ and __eventually__ retry."
    assert (
        describe.plain_summary(text)
        == "Fixed test_retry.py; set DATABASE_URL and eventually retry."
    )


def test_every_state_can_finish():
    for start in ThreadState:
        seen, frontier = {start}, [start]
        while frontier:
            for nxt in TRANSITIONS[frontier.pop()] - seen:
                seen.add(nxt)
                frontier.append(nxt)
        assert seen & FINISHED_STATES, start


def test_status_outcome_and_turns_serialize_and_restore():
    running = make_handle(ThreadState.EXECUTING)
    assert running.status == "running" and running.outcome == ""

    paused = make_handle(ThreadState.AWAITING_PERMISSION)
    assert paused.status == "awaiting_approval" and paused.outcome == ""

    done = make_handle(ThreadState.COMPLETED)
    done.turns = [
        Turn(
            seq=1,
            prompt="Add greet()",
            outcome="finished",
            answer="Added greet().",
            started_at=10.0,
            ended_at=12.0,
        )
    ]
    assert done.status == "idle" and done.outcome == "finished"
    snapshot = done.to_dict()
    snapshot["announced_" + "permission_key"] = "k1"
    restored = ThreadHandle.from_dict(snapshot)
    assert len(restored.turns) == 1
    assert restored.turns[0].prompt == "Add greet()"
    assert restored.turns[0].answer == "Added greet()."
    assert restored.turns[0].outcome == "finished"


@pytest.mark.asyncio
async def test_illegal_transitions_raise(registry: ThreadRegistry):
    handle = make_handle(ThreadState.COMPLETED)
    with pytest.raises(InvalidTransition):
        await registry.transition(handle, ThreadState.AWAITING_PERMISSION)
    await registry.transition(handle, ThreadState.EXECUTING)
    assert handle.state is ThreadState.EXECUTING and handle.ended_at is None


@pytest.mark.asyncio
async def test_a_milestone_is_delivered_once(registry: ThreadRegistry):
    updates, live = ThreadUpdates(), FakeLiveQueue()
    updates.attach(registry, live)
    handle = make_handle()

    waiter = asyncio.create_task(updates.wait(handle, 0))
    await asyncio.sleep(0)
    gate = await registry.emit(
        handle, EventKind.PERMISSION_REQUESTED, "Needs approval"
    )
    assert await asyncio.wait_for(waiter, 5) == gate
    assert live.sent == [], (
        "the waiting tool call announces it; the session must not"
    )

    await registry.emit(
        handle, EventKind.PROGRESS, "narration is not announced"
    )
    await registry.emit(handle, EventKind.ERROR, "Boom")
    assert len(live.sent) == 1 and "failed: Boom" in live.sent[0]
    with pytest.raises(TimeoutError):  # both milestones were delivered already
        await asyncio.wait_for(updates.wait(handle, 0), 0.2)


@pytest.mark.asyncio
async def test_a_newer_waiter_supersedes_an_older_one(registry: ThreadRegistry):
    updates = ThreadUpdates()
    handle = make_handle()
    older = asyncio.create_task(updates.wait(handle, 0))
    await asyncio.sleep(0)
    newer = asyncio.create_task(updates.wait(handle, 0))
    await asyncio.sleep(0)

    event = await registry.emit(
        handle, EventKind.PERMISSION_REQUESTED, "Needs approval"
    )
    assert await asyncio.wait_for(older, 5) is None
    assert await asyncio.wait_for(newer, 5) == event


@pytest.mark.asyncio
async def test_dismissed_and_cancelled_threads_are_not_announced(
    registry: ThreadRegistry,
):
    updates, live = ThreadUpdates(), FakeLiveQueue()
    updates.attach(registry, live)
    handle = make_handle()
    await registry.emit(handle, EventKind.CANCELLED, "Cancelled.")
    handle.dismissed = True
    await registry.emit(handle, EventKind.COMPLETED, "Done")
    assert live.sent == []


def test_new_thread_id_uses_16_hex_chars():
    from app.threads.model import new_thread_id

    tid = new_thread_id()
    assert tid.startswith("thread-")
    suffix = tid.removeprefix("thread-")
    assert len(suffix) == 16 and all(c in "0123456789abcdef" for c in suffix)


def test_raw_diff_is_not_persisted_on_every_turn():
    handle = make_handle(ThreadState.COMPLETED)
    handle.raw_diff = "diff --git a/x b/x"
    handle.sync_active_turn()
    snap = handle.to_dict()
    assert snap["raw_diff"] == "diff --git a/x b/x"
    assert "raw_diff" not in snap["turns"][0]
    restored = ThreadHandle.from_dict(snap)
    assert restored.turns[-1].raw_diff == "diff --git a/x b/x"


@pytest.mark.asyncio
async def test_wait_returns_none_when_cancelled_and_cancelled_ignores_late_complete(
    registry: ThreadRegistry,
):
    updates = ThreadUpdates()
    handle = make_handle(ThreadState.EXECUTING)
    waiter = asyncio.create_task(updates.wait(handle, 0))
    await asyncio.sleep(0)
    await registry.transition(handle, ThreadState.CANCELLED)
    await registry.emit(handle, EventKind.CANCELLED, "Cancelled.")
    assert await asyncio.wait_for(waiter, 1.0) is None
    # Late driver completion after cancel must not raise InvalidTransition.
    await registry.complete(handle, {})
    await registry.fail(handle, "late failure")
    assert handle.state is ThreadState.CANCELLED
