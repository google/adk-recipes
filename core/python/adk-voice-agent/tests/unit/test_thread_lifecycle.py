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

"""Thread lifecycle, multi-turn iterations, and turn summaries in the same workspace.

Each test drives the public thread API (dispatch / steer / approve) against the real
thread stack with a scripted harness CLI, and checks the invariants the voice layer
relies on: iterations continue on the thread's branch, sessions resume without repeating
the goal, and history (including turns and summaries) survives a restart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api.serializers import thread_detail, thread_summary
from app.threads.model import (
    EventKind,
    Instruction,
    InstructionKind,
    ThreadState,
)
from app.threads.registry import ThreadRegistry
from app.threads.summaries import (
    fallback_summary,
    set_turn_summarizer,
    summarize_turn,
)
from app.voice.thread_updates import format_milestone
from app.workers import runs
from tests.fakes import GOAL, REPO, Lab, git, kinds

pytestmark = pytest.mark.asyncio

REPORT = (
    "ADK Voice Agent is tested in four layers. The offline suite passed.\n\n"
    "## The layers\n\n| Layer | Command |\n|---|---|\n| Offline | make test |\n"
)


async def test_execute_and_commit_on_branch(lab: Lab):
    lab.agent.script(
        {
            "steps": [
                {"say": "Writing greet()"},
                {"write": {"greet.py": "def greet():\n    return 'hi'\n"}},
            ]
        },
    )

    handle = await lab.dispatch()
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert handle.files_changed == ["greet.py"]
    assert handle.workdir is not None
    assert (
        handle.commit_sha
        and git(
            handle.workdir, "log", "-1", "--format=%H", handle.branch
        ).strip()
        == handle.commit_sha
    )
    assert not handle.pushed  # the fixture repo has no remote
    first_call = lab.agent.calls[0]
    assert first_call["prompt"] == GOAL and first_call["resume"] is None
    assert "progress" in kinds(handle) and "activity" in kinds(handle)


async def test_follow_up_after_completion_starts_a_new_iteration(lab: Lab):
    lab.agent.script(
        {"steps": [{"write": {"greet.py": "def greet():\n    return 'hi'\n"}}]},
        {"steps": [{"write": {"test_greet.py": "from greet import greet\n"}}]},
    )
    handle = await lab.dispatch()
    assert handle.state is ThreadState.COMPLETED

    await lab.registry.steer(handle.thread_id, "Also add a test")
    handle = await lab.settle(handle.thread_id)
    assert handle.iteration == 2
    assert handle.state is ThreadState.COMPLETED
    followup = lab.agent.calls[1]
    assert followup["resume"] == lab.agent.calls[0]["session"]
    assert followup["prompt"] == "Also add a test"
    assert GOAL not in followup["prompt"]
    assert handle.instructions[-1] == Instruction(
        InstructionKind.FOLLOWUP, "Also add a test", 2
    )
    assert handle.files_changed == ["test_greet.py"], (
        "changes are measured per iteration"
    )
    assert handle.workdir is not None
    assert git(
        handle.workdir, "ls-tree", "--name-only", handle.branch
    ).split() == [
        "README.md",
        "greet.py",
        "test_greet.py",
    ], "the branch keeps earlier work"
    assert git(
        handle.workdir, "log", "--format=%s", handle.branch
    ).splitlines() == [
        f"thread {handle.thread_id}: {handle.title}",
        f"thread {handle.thread_id}: {handle.title}",
        "Seed repository",
    ]  # one commit per iteration


async def test_a_runs_mcp_token_stops_working_when_the_run_ends(lab: Lab):
    from app.api import mcp

    minted: list[str] = []
    real_mint = mcp.mint_mcp_token

    def recording_mint(thread_id: str, user_id: str) -> str:
        minted.append(token := real_mint(thread_id, user_id))
        return token

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp, "mint_mcp_token", recording_mint)
        lab.agent.script(
            {"steps": [{"write": {"greet.py": "x = 1\n"}}]},
            {"steps": [{"write": {"b.py": "y = 2\n"}}]},
        )
        handle = await lab.dispatch()
        await lab.registry.steer(handle.thread_id, "Add b.py")
        handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED
    assert len(minted) == 2  # one per run
    assert all(mcp.verify_mcp_token(token) is None for token in minted)


async def test_lost_session_restarts_fresh_with_a_handoff(lab: Lab):
    lab.agent.script({}, {"resume_fails": True}, {})
    handle = await lab.dispatch()
    first_session = handle.sessions["claude"]
    await lab.registry.steer(handle.thread_id, "Add a docstring")
    handle = await lab.settle(handle.thread_id)

    assert handle.state is ThreadState.COMPLETED, handle.error
    retry = lab.agent.calls[2]
    assert retry["resume"] is None
    assert (
        "[CONTEXT FROM EARLIER WORK ON THIS THREAD]" in retry["prompt"]
        and GOAL in retry["prompt"]
    )
    assert handle.sessions["claude"] == retry["session"] != first_session


async def test_switching_harness_hands_off_in_the_same_workspace(lab: Lab):
    lab.agent.script({}, {})
    handle = await lab.dispatch()
    clone = handle.workdir

    await lab.registry.steer(
        handle.thread_id, "Prefer pathlib", harness="antigravity"
    )
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED
    assert handle.harness == "antigravity" and handle.workdir == clone
    agy = lab.agent.calls[1]
    assert agy["dialect"] == "agy" and agy["resume"] is None
    assert GOAL in agy["prompt"] and "Prefer pathlib" in agy["prompt"]
    assert set(handle.sessions) == {"claude", "antigravity"}


async def test_milestones_wake_waiters(lab: Lab):
    lab.agent.script({})
    handle = await lab.registry.dispatch(GOAL, REPO)
    event = await handle.wait_for_milestone(0, timeout_s=20)
    assert event is not None and event.kind == "completed"
    assert await handle.wait_for_milestone(event.seq, timeout_s=0.2) is None


async def test_history_survives_a_restart(lab: Lab):
    lab.agent.script({"steps": [{"write": {"greet.py": "x = 1\n"}}]})
    handle = await lab.dispatch()

    restarted = ThreadRegistry(lab.store)
    loaded = await restarted.get(handle.thread_id)
    assert loaded is not None and loaded is not handle
    assert loaded.state is ThreadState.COMPLETED
    assert loaded.sessions == handle.sessions
    assert loaded.files_changed == ["greet.py"]
    assert len(loaded.turns) == 1 and loaded.turns[0].prompt == GOAL
    assert [e.seq for e in loaded.events] == [e.seq for e in handle.events]
    assert [t.thread_id for t in await restarted.list_all()] == [
        handle.thread_id
    ]

    briefing = await restarted.unseen_milestones()
    assert [e["kind"] for e in briefing] == ["completed"]
    assert await restarted.unseen_milestones() == []


async def test_threads_interrupted_by_a_restart_are_failed(lab: Lab):
    lab.agent.script({"steps": [{"hang": True}]})
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.wait_for_calls(1)

    restarted = ThreadRegistry(lab.store)
    assert await restarted.recover_interrupted() == 1
    loaded = await restarted.get(handle.thread_id)
    assert loaded is not None and loaded.error is not None
    assert (
        loaded.state is ThreadState.FAILED and "server restart" in loaded.error
    )

    # The orphaned harness process is stopped, and the briefing mentions the failure.
    assert restarted.orphan_cleanup is not None
    await restarted.orphan_cleanup
    runs_root = Path(runs.run_dir_for(lab.env, "x")).parent
    (run_dir,) = runs_root.glob(f"{handle.thread_id}-*")
    assert lab.exit_code(run_dir.name) == -15
    # (The old registry, still alive in this test, also reports the killed run.)
    briefing = await restarted.unseen_milestones()
    assert ("error", handle.thread_id) in [
        (e["kind"], e["thread_id"])
        for e in briefing
        if "server restart" in e["message"]
    ]


async def test_concurrent_first_use_of_a_fresh_store(tmp_path: Path):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.store.thread_store import ThreadStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    store = ThreadStore(engine)
    await asyncio.gather(*(store.recent_thread_ids("local") for _ in range(5)))
    await engine.dispose()


# ---- turn summaries ---------------------------------------------------------


async def test_a_finished_turn_is_summarized_and_the_voice_speaks_it(lab: Lab):
    seen: list[tuple[str, str]] = []

    async def summarize(prompt: str, answer: str) -> str:
        seen.append((prompt, answer))
        return "Four layers of tests, and the offline suite passed."

    set_turn_summarizer(summarize)
    lab.agent.script({"steps": [{"say": "Reading tests"}], "result": REPORT})

    handle = await lab.dispatch()

    assert handle.state is ThreadState.COMPLETED, handle.error
    assert seen == [(handle.goal, REPORT)]
    summary = "Four layers of tests, and the offline suite passed."
    assert handle.summary == summary
    assert handle.turns[-1].summary == summary
    assert thread_summary(handle)["summary"] == summary
    assert thread_detail(handle)["turns"][-1]["summary"] == summary
    completed = next(e for e in handle.events if e.kind is EventKind.COMPLETED)
    assert completed.data["summary"] == summary
    spoken = format_milestone(handle, completed)
    assert f"finished: {summary}" in spoken
    assert "as written" in spoken


async def test_a_follow_up_clears_the_last_summary_until_it_finishes(lab: Lab):
    lab.agent.script({"result": REPORT}, {"result": "Added a test."})
    handle = await lab.dispatch()
    assert handle.summary

    await lab.registry.steer(handle.thread_id, "Also add a test")
    handle = await lab.settle(handle.thread_id)

    assert handle.summary == "Added a test."
    assert [t.summary for t in handle.turns] == [
        fallback_summary(REPORT),
        "Added a test.",
    ]


async def test_a_failing_summarizer_falls_back_to_the_first_paragraph():
    async def broken(prompt: str, answer: str) -> str:
        raise RuntimeError("no model")

    set_turn_summarizer(broken)

    assert await summarize_turn("Look into the tests", REPORT) == (
        "ADK Voice Agent is tested in four layers. The offline suite passed."
    )
    assert await summarize_turn("Look into the tests", "   ") == ""
