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

"""Voice thread tools: the guards that keep the live model inside the thread contract."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.auth.identity import run_as
from app.threads.model import EventKind, ThreadState
from app.threads.registry import DUPLICATE_WINDOW_S
from app.tools.thread_tools import (
    SINCE_SEQ_KEY,
    approve,
    cancel_thread,
    list_threads,
    message_thread,
    publish_thread,
    read_thread,
    start_thread,
)
from tests.fakes import GOAL, REPO, Lab, git

pytestmark = pytest.mark.asyncio

GREET = {"write": {"greet.py": "def greet():\n    return 'hi'\n"}}


async def test_decline_requires_a_pending_permission_request(lab: Lab):
    started = await start_thread(GOAL, REPO)
    handle = await lab.settle(started["thread_id"])
    assert handle.state is ThreadState.COMPLETED

    declined = await approve(handle.thread_id, approved=False)
    assert declined["ok"] is False
    assert "nothing to decline" in declined["message"].lower()
    assert handle.state is ThreadState.COMPLETED
    assert len(handle.turns) == 1


async def test_identical_dispatches_in_one_turn_start_one_thread(lab: Lab):
    first, second = await asyncio.gather(
        start_thread(GOAL, REPO), start_thread(GOAL, REPO)
    )
    assert first["thread_id"] == second["thread_id"]
    assert len(await lab.registry.list_all()) == 1
    await lab.settle(first["thread_id"])


async def test_each_user_has_their_own_dispatch_lock(lab: Lab):
    """The lock lives on the registry, one per user, so it never outlives its loop."""
    registry = lab.registry
    assert registry.dispatch_lock() is registry.dispatch_lock()
    with run_as("someone-else@example.com"):
        theirs = registry.dispatch_lock()
    assert theirs is not registry.dispatch_lock()


async def test_a_repeated_request_gets_the_running_thread_back(lab: Lab):
    """Same user, goal (case and spacing aside), and project while the first is active
    and recent: the registry hands it back. Anything else is a new thread."""
    lab.agent.script({"steps": [{"run": "git push origin main"}]})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.settle(handle.thread_id)  # waiting on permission: still active
    registry, project = lab.registry, handle.project

    assert registry.recent_duplicate(f"  {GOAL.upper()} ", project) is handle
    assert registry.recent_duplicate(GOAL, None) is handle, "any project"
    assert registry.recent_duplicate(GOAL, "projects/other") is None
    assert registry.recent_duplicate("Something else", project) is None
    with run_as("someone-else@example.com"):
        assert registry.recent_duplicate(GOAL, project) is None

    reply = await start_thread(GOAL, REPO)
    assert (
        reply["thread_id"] == handle.thread_id
        and "not starting another" in reply["message"]
    )

    handle.created_at -= DUPLICATE_WINDOW_S + 1
    assert registry.recent_duplicate(GOAL, project) is None, (
        "only within the window"
    )
    handle.created_at += DUPLICATE_WINDOW_S + 1
    await registry.cancel(handle.thread_id)
    assert registry.recent_duplicate(GOAL, project) is None, (
        "only while it is active"
    )


async def test_dispatch_reports_invalid_requests_instead_of_raising(lab: Lab):
    assert (await start_thread("  ", REPO))["message"].startswith(
        "Could not start the thread"
    )
    assert (
        "Unknown coding harness"
        in (await start_thread(GOAL, REPO, harness="nope"))["message"]
    )


async def test_thread_listing_names_repo_and_goal(lab: Lab):
    assert await list_threads() == "There are no threads."
    lab.agent.script({"steps": [{"run": "git push origin main"}]})
    started = await start_thread(GOAL, REPO)
    await lab.settle(started["thread_id"])
    listing = await list_threads()
    title = (await lab.registry.require(started["thread_id"])).title
    assert (
        f'{started["thread_id"]} "{title}" in code/{REPO} (asked: "{GOAL}")'
        in listing
    )
    assert listing.startswith("Waiting on you:")


async def test_thread_listing_searches_by_description(lab: Lab):
    started = await start_thread(GOAL, REPO)
    await lab.settle(started["thread_id"])
    assert started["thread_id"] in await list_threads("the greeting helper")
    assert await list_threads("telemetry migration") == (
        "No threads match 'telemetry migration'."
    )


async def test_steering_replies_with_what_happens_next(lab: Lab):
    started = await start_thread(GOAL, REPO)
    await lab.settle(started["thread_id"])

    continued = await message_thread(started["thread_id"], "Use f-strings")
    title = (await lab.registry.require(started["thread_id"])).title
    assert continued["message"] == f"Started round 2 of “{title}”."
    assert continued[SINCE_SEQ_KEY] > 0
    await lab.settle(started["thread_id"])
    assert (await message_thread("thread-missing", "x")) == {
        "message": "No thread with ID thread-missing."
    }


async def test_thread_result_reads_the_permission_gate_then_the_outcome(
    lab: Lab,
):
    lab.agent.script(
        {"steps": [{"run": "git push origin main"}]},
        {"steps": [{"write": {"greet.py": "x = 1\n"}}]},
    )
    started = await start_thread(GOAL, REPO)
    thread_id = started["thread_id"]
    await lab.settle(thread_id)

    blocked = await read_thread(thread_id)
    assert (
        "Waiting for permission to run `git push origin main` (merge)."
        in blocked
        and "Next:" in blocked
    )

    await approve(thread_id)
    await lab.settle(thread_id)
    outcome = await read_thread(thread_id)
    assert "Outcome: Done. Made the change and ran the tests." in outcome
    assert (
        "not pushed)." in outcome
        and "in the folder code/voice-agent-fixture: greet.py." in outcome
    )
    assert "Diff excerpt:" in outcome and "+x = 1" in outcome
    assert (
        await read_thread("thread-missing")
        == "No thread with ID thread-missing."
    )


async def test_thread_result_surfaces_preliminary_progress_while_running(
    lab: Lab,
):
    lab.agent.script({"steps": [{"hang": True}]})
    started = await start_thread(GOAL, REPO)
    await lab.wait_for_calls(1)
    handle = await lab.registry.require(started["thread_id"])
    handle.record(EventKind.PROGRESS, "Spotted Southwest 11:30 AM ($326)")
    handle.record(
        EventKind.ACTIVITY,
        "Checking hotel cancellation policies for Silver Lake Pool & Inn",
    )

    running = await read_thread(handle.thread_id)
    assert (
        "Preliminary progress so far (still running):" in running
        and "Spotted Southwest 11:30 AM ($326)" in running
        and "Latest: Checking hotel cancellation policies for Silver Lake Pool & Inn"
        in running
    )
    assert (
        "share any preliminary progress so far while noting what is still running"
        in running
    )
    await cancel_thread(handle.thread_id)


async def test_cancel_keeps_the_branch(lab: Lab):
    lab.agent.script({"steps": [{"hang": True}]})
    started = await start_thread(GOAL, REPO)
    await lab.wait_for_calls(1)

    reply = await cancel_thread(started["thread_id"])
    title = (await lab.registry.require(started["thread_id"])).title
    assert reply == (
        f"Cancelled “{title}”. Its branch agent/{started['thread_id']} is kept."
    )
    assert "already cancelled" in await cancel_thread(started["thread_id"])


async def test_publish_thread_pushes_the_branch_only_when_asked(
    lab: Lab, tmp_path: Path
):
    lab.agent.script(
        {"steps": [GREET]}, {"steps": [{"write": {"b.py": "b = 1\n"}}]}
    )
    handle = await lab.dispatch()
    assert handle.state is ThreadState.COMPLETED, handle.error
    root = Path(lab.env.workspace_root) / "code" / REPO
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    git(str(root), "remote", "add", "origin", str(origin))

    # A round that finishes with a remote configured still pushes nothing.
    await lab.registry.steer(handle.thread_id, "Add b")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert git(str(origin), "branch", "--list", handle.branch) == ""
    assert not handle.pushed

    reply = await publish_thread(handle.thread_id)
    assert reply.startswith("Pushed") and handle.branch in reply
    assert handle.pushed
    assert (
        git(str(origin), "rev-parse", handle.branch).strip()
        == handle.commit_sha
    )
    assert any(e.data.get("pushed") for e in handle.events)


async def test_publish_thread_refuses_threads_without_a_branch(lab: Lab):
    lab.agent.script({})
    handle = await lab.dispatch(GOAL, "projects/greetings")
    reply = await publish_thread(handle.thread_id)
    assert reply.startswith("Nothing pushed") and "no branch" in reply
