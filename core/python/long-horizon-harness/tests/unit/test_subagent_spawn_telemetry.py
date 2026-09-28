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

"""Background ``agent(spawn)`` sub-agents must surface in the Agents panel.

The panel reads ``delegate_history``; before this, only blocking ``delegate()``
wrote it, so fire-and-forget spawns were invisible. ``spawn`` now records a
``running`` row and ``status``/``result`` flip it to a terminal status.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.telemetry.ui import DELEGATE_HISTORY_STATE_KEY

pytestmark = pytest.mark.asyncio


class _FakeToolContext:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}


def _hist(tc: _FakeToolContext) -> list[dict[str, Any]]:
    return tc.state.get(DELEGATE_HISTORY_STATE_KEY) or []


async def test_spawn_records_running_row(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "completed", "summary": "done", "telemetry": {}}

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    tc = _FakeToolContext()
    spawn = await agent(action="spawn", goal="enumerate docs", tool_context=tc)

    rows = _hist(tc)
    assert rows, "spawn must record a row so the agent shows in the panel"
    assert rows[-1]["task_id"] == spawn["task_id"]
    assert rows[-1]["status"] == "running"
    assert "enumerate docs" in rows[-1]["goal"]


async def test_result_flips_row_to_completed(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "summary": "found 3 files",
            "telemetry": {},
        }

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    tc = _FakeToolContext()  # same context across calls == same persisted state
    spawn = await agent(action="spawn", goal="x", tool_context=tc)
    await agent(
        action="result",
        task_id=spawn["task_id"],
        wait=True,
        timeout_s=5.0,
        tool_context=tc,
    )

    rows = _hist(tc)
    assert len(rows) == 1, "result must update the existing row, not append"
    assert rows[0]["status"] == "completed"
    assert "found 3 files" in rows[0]["summary"]


async def test_result_failure_flips_row_to_halted(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        raise KeyError("not-a-toolset")

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)

    tc = _FakeToolContext()
    spawn = await agent(
        action="spawn", goal="x", toolsets=["not-a-toolset"], tool_context=tc
    )
    await agent(
        action="result",
        task_id=spawn["task_id"],
        wait=True,
        timeout_s=5.0,
        tool_context=tc,
    )

    rows = _hist(tc)
    assert rows[0]["status"] == "halted"


async def test_spawn_without_tool_context_is_noop(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "completed", "summary": "ok", "telemetry": {}}

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    spawn = await agent(action="spawn", goal="x")  # no tool_context
    assert spawn["success"] is True  # must not raise
