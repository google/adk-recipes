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

"""delegate() forwards profile + parent grants down to build_child_agent."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state


async def test_delegate_forwards_profile_and_grants(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_child(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "completed", "summary": "ok", "telemetry": {}}

    from app.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY
    from app.subagents import delegate as delegate_mod

    monkeypatch.setattr(delegate_mod, "run_child", _fake_run_child)

    grant = {"tool_name": "terminal", "signature": {"command": "ls"}}
    ctx = _Ctx({POLICY_GRANTS_STATE_KEY: [grant]})

    await delegate_mod.delegate(goal="g", profile="explore", tool_context=ctx)

    assert captured["profile"] == "explore"
    assert captured["parent_grants"] == [grant]


async def test_delegate_no_grants_when_state_empty(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_child(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "completed", "summary": "ok", "telemetry": {}}

    from app.subagents import delegate as delegate_mod

    monkeypatch.setattr(delegate_mod, "run_child", _fake_run_child)
    await delegate_mod.delegate(goal="g", tool_context=_Ctx({}))

    assert captured["profile"] is None
    assert captured["parent_grants"] == []


async def test_spawn_forwards_profile_no_grants(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run_child(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "completed", "summary": "ok", "telemetry": {}}

    from app.subagents import spawn as spawn_mod
    from app.subagents.registry import reset_registry

    reset_registry()
    monkeypatch.setattr(spawn_mod, "run_child", _fake_run_child)

    res = await spawn_mod.agent(action="spawn", goal="g", profile="explore")
    assert res["success"] is True
    # Let the background task run.
    import asyncio

    await asyncio.sleep(0.05)
    assert captured["profile"] == "explore"
    assert captured["parent_grants"] is None
    reset_registry()
