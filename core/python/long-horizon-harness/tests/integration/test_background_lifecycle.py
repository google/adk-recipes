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

"""End-to-end lifecycle of the background-process surface, exercising
the real ``bash`` and ``process`` tools against ``LocalEnvironment``
(no LLM in the loop).

Covers the three flows the model is taught to drive:

  1. Explicit ``background=True`` spawn → registry lookup → kill.
  2. Auto-promote (``timeout_s=1`` on a sleep that outlives it) → the
     handle is registered and ``process(wait)`` returns its exit.
  3. Interactive recovery: ``process(write)`` unblocks a ``read``-waiting
     command and the next ``poll`` shows the line was consumed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from horizon.environment.registry import ProcessRegistry
from horizon.tools.processes.process import process
from horizon.tools.processes.terminal import bash

pytestmark = pytest.mark.asyncio


def _ctx() -> SimpleNamespace:
    """Minimal tool_context surrogate: just exposes ``.state`` dict that
    the ProcessRegistry keys off."""
    return SimpleNamespace(state={})


async def test_background_spawn_poll_kill_round_trip() -> None:
    ctx = _ctx()
    spawn = await process(
        action="spawn",
        command="sleep 30",
        tool_context=ctx,
    )
    assert spawn["status"] == "running"
    sid = spawn["session_id"]

    listing = await process(action="list", tool_context=ctx)
    assert any(s["session_id"] == sid for s in listing["running"])

    poll = await process(action="poll", session_id=sid, tool_context=ctx)
    assert poll["status"] == "running"

    killed = await process(action="kill", session_id=sid, tool_context=ctx)
    assert killed["status"] == "killed"

    # The registry keeps finished sessions around briefly so the model can
    # still inspect exit state.
    after = await process(action="poll", session_id=sid, tool_context=ctx)
    assert after["status"] == "exited"


async def test_auto_promote_on_timeout_preserves_session() -> None:
    ctx = _ctx()
    result = await bash(
        command="sleep 3 && echo done",
        timeout_s=1,
        tool_context=ctx,
    )

    assert result["timed_out"] is True
    assert result["status"] == "running"
    sid = result["session_id"]

    final = await process(
        action="wait", session_id=sid, timeout_s=10, tool_context=ctx
    )
    assert final["status"] == "exited"
    assert final["exit_code"] == 0

    poll = await process(action="poll", session_id=sid, tool_context=ctx)
    assert "done" in poll["tail"]


async def test_write_unblocks_interactive_session() -> None:
    ctx = _ctx()
    spawn = await process(
        action="spawn",
        command="bash -c 'read line; echo got=$line'",
        tool_context=ctx,
    )
    sid = spawn["session_id"]

    await process(action="write", session_id=sid, data="hi", tool_context=ctx)
    final = await process(
        action="wait", session_id=sid, timeout_s=5, tool_context=ctx
    )
    assert final["exit_code"] == 0

    poll = await process(action="poll", session_id=sid, tool_context=ctx)
    assert "got=hi" in poll["tail"]


async def test_registry_isolated_per_session() -> None:
    """Two distinct tool_contexts (i.e. two ADK sessions) must not see
    each other's processes — the registry is scoped to ``state``."""
    ctx_a = _ctx()
    ctx_b = _ctx()

    spawn_a = await process(
        action="spawn", command="sleep 30", tool_context=ctx_a
    )
    spawn_b = await process(
        action="spawn", command="sleep 30", tool_context=ctx_b
    )

    sids_a = {
        s["session_id"]
        for s in (await process(action="list", tool_context=ctx_a))["running"]
    }
    sids_b = {
        s["session_id"]
        for s in (await process(action="list", tool_context=ctx_b))["running"]
    }

    assert spawn_a["session_id"] in sids_a
    assert spawn_a["session_id"] not in sids_b
    assert spawn_b["session_id"] in sids_b
    assert spawn_b["session_id"] not in sids_a

    # cleanup so the daemon readers don't outlive the test
    for sid in sids_a:
        await process(action="kill", session_id=sid, tool_context=ctx_a)
    for sid in sids_b:
        await process(action="kill", session_id=sid, tool_context=ctx_b)


async def test_registry_for_state_stable_key() -> None:
    """Sanity check: ``ProcessRegistry.for_state`` returns the same
    registry for the same state dict, so subsequent tool calls within a
    session see the same backing store."""
    state: dict = {}
    a = ProcessRegistry.for_state(state)
    b = ProcessRegistry.for_state(state)
    assert a is b
