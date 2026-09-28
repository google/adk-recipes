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

"""``agent(action='spawn'|'status'|'result'|'cancel'|'list')`` dispatch —
the model-facing wrapper around the sub-agent registry.

All tests stub ``build_child_agent`` and ``run_delegate`` so no real
Agent or Runner is constructed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


async def test_spawn_returns_task_id_and_status(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(
        *, child: Any, user_message: str, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        del child, user_message, timeout_s
        return {"status": "completed", "summary": "child-done", "telemetry": {}}

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    spawn = await agent(action="spawn", goal="do thing")
    assert spawn["success"] is True
    task_id = spawn["task_id"]
    assert task_id

    result = await agent(
        action="result", task_id=task_id, wait=True, timeout_s=5.0
    )
    assert result["status"] == "completed"
    assert result["result"]["summary"] == "child-done"

    final = await agent(action="status", task_id=task_id)
    assert final["status"] == "completed"


async def test_result_no_wait_returns_pending(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    release = asyncio.Event()

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(
        *, child: Any, user_message: str, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        del child, user_message, timeout_s
        await release.wait()
        return {"status": "completed", "summary": "x", "telemetry": {}}

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    spawn = await agent(action="spawn", goal="slow")
    out = await agent(action="result", task_id=spawn["task_id"], wait=False)
    assert out["status"] in {"pending", "running"}

    release.set()
    await agent(
        action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
    )


async def test_cancel(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    started = asyncio.Event()

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(
        *, child: Any, user_message: str, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        del child, user_message, timeout_s
        started.set()
        await asyncio.sleep(60)
        return {"status": "completed", "summary": "", "telemetry": {}}

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    spawn = await agent(action="spawn", goal="forever")
    await started.wait()

    out = await agent(action="cancel", task_id=spawn["task_id"])
    assert out["cancelled"] is True

    for _ in range(50):
        status = await agent(action="status", task_id=spawn["task_id"])
        if status["status"] == "cancelled":
            break
        await asyncio.sleep(0.01)
    assert status["status"] == "cancelled"


async def test_delegate_wrapper_still_returns_run_envelope(monkeypatch):
    """delegate() should keep its current shape — backwards compat."""
    from app.subagents import delegate as delegate_module

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(
        *, child: Any, user_message: str, timeout_s: float = 120.0
    ) -> dict[str, Any]:
        del child, user_message, timeout_s
        return {
            "status": "completed",
            "summary": "wrapped-result",
            "telemetry": {
                "iterations": 1,
                "duration_ms": 1,
                "invocation_id": "x",
            },
        }

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    out = await delegate_module.delegate(goal="ship")
    assert out["status"] == "completed"
    assert out["summary"] == "wrapped-result"
    assert "telemetry" in out


async def test_unknown_toolset_returns_error_envelope(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        raise KeyError("not-a-toolset")

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)

    spawn = await agent(action="spawn", goal="x", toolsets=["not-a-toolset"])
    assert spawn["success"] is True
    out = await agent(
        action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
    )
    assert out["status"] == "failed"
    assert "not-a-toolset" in out.get("error", "")


# =============================================================================
# wait action
# =============================================================================


async def test_wait_dispatch_returns_finished(monkeypatch):
    from app.subagents import delegate as delegate_module
    from app.subagents.spawn import agent

    async def _fake_build(**_kwargs: Any) -> Any:
        return object()

    async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "summary": "waited-done",
            "telemetry": {},
        }

    monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
    monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

    spawn = await agent(action="spawn", goal="quick")
    out = await agent(action="wait", task_ids=[spawn["task_id"]], timeout_s=5.0)
    assert out["task_id"] == spawn["task_id"]
    assert out["status"] == "completed"
    assert out["result"]["summary"] == "waited-done"


async def test_wait_unknown_scope_returns_no_active():
    from app.subagents.spawn import agent

    out = await agent(action="wait", task_ids=["does-not-exist"], timeout_s=0.1)
    assert out["status"] == "no_active_tasks"


# =============================================================================
# Dispatch error branches
# =============================================================================


class TestDispatchErrors:
    async def test_spawn_missing_goal_rejected(self):
        from app.subagents.spawn import agent

        result = await agent(action="spawn")
        assert result["success"] is False
        assert "goal" in result["error"].lower()

    async def test_status_missing_task_id_rejected(self):
        from app.subagents.spawn import agent

        result = await agent(action="status")
        assert result["success"] is False
        assert "task_id" in result["error"]

    async def test_result_missing_task_id_rejected(self):
        from app.subagents.spawn import agent

        result = await agent(action="result")
        assert result["success"] is False
        assert "task_id" in result["error"]

    async def test_cancel_missing_task_id_rejected(self):
        from app.subagents.spawn import agent

        result = await agent(action="cancel")
        assert result["success"] is False
        assert "task_id" in result["error"]

    async def test_unknown_action_rejected(self):
        from app.subagents.spawn import agent

        result = await agent(action="banana")  # type: ignore[arg-type]
        assert result["success"] is False
        assert "action" in result["error"].lower()


# =============================================================================
# Wider API parity: spawn accepts the same knobs as delegate()
# =============================================================================


class TestSpawnWiderApi:
    async def test_spawn_forwards_model_override(self, monkeypatch):
        from app.subagents import delegate as delegate_module
        from app.subagents.spawn import agent

        captured: dict[str, Any] = {}

        async def _fake_build(**kwargs: Any) -> Any:
            captured["kwargs"] = kwargs
            return object()

        async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
            return {"status": "completed", "summary": "ok", "telemetry": {}}

        monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
        monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

        spawn = await agent(
            action="spawn", goal="deep think", model="gemini-3.1-pro-preview"
        )
        await agent(
            action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
        )
        assert captured["kwargs"].get("model_name") == "gemini-3.1-pro-preview"

    async def test_spawn_forwards_tools_and_instructions_and_inline_skills(
        self, monkeypatch
    ):
        from app.subagents import delegate as delegate_module
        from app.subagents.spawn import agent

        captured: dict[str, Any] = {}

        async def _fake_build(**kwargs: Any) -> Any:
            captured["kwargs"] = kwargs
            return object()

        async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
            return {"status": "completed", "summary": "ok", "telemetry": {}}

        monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
        monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

        spawn = await agent(
            action="spawn",
            goal="x",
            tools=["read_file"],
            instructions="Be terse.",
            inline_skills=[{"name": "ad-hoc", "body": "Echo cwd first."}],
        )
        await agent(
            action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
        )
        assert captured["kwargs"].get("tools") == ["read_file"]
        assert captured["kwargs"].get("instructions") == "Be terse."
        assert captured["kwargs"].get("inline_skills") == [
            {"name": "ad-hoc", "body": "Echo cwd first."}
        ]

    async def test_spawn_forwards_max_iterations_to_runner(self, monkeypatch):
        from app.subagents import delegate as delegate_module
        from app.subagents.spawn import agent

        captured: dict[str, Any] = {}

        async def _fake_build(**_kwargs: Any) -> Any:
            return object()

        async def _fake_run(**kwargs: Any) -> dict[str, Any]:
            captured["kwargs"] = kwargs
            return {"status": "completed", "summary": "ok", "telemetry": {}}

        monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
        monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

        spawn = await agent(action="spawn", goal="x", max_iterations=7)
        await agent(
            action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
        )
        assert captured["kwargs"].get("max_iterations") == 7

    async def test_spawn_custom_name_appears_in_list_and_status(
        self, monkeypatch
    ):
        """A caller-supplied ``name`` surfaces in registry status / list
        envelopes so the parent can correlate task_ids with intent."""
        import asyncio

        from app.subagents import delegate as delegate_module
        from app.subagents.spawn import agent

        release = asyncio.Event()

        async def _fake_build(**_kwargs: Any) -> Any:
            return object()

        async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
            await release.wait()
            return {"status": "completed", "summary": "x", "telemetry": {}}

        monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
        monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

        spawn = await agent(action="spawn", goal="x", name="enumerate_docs")
        status = await agent(action="status", task_id=spawn["task_id"])
        assert "enumerate_docs" in status.get("name", "")

        listing = await agent(action="list")
        names = [a.get("name", "") for a in listing["active"]]
        assert any("enumerate_docs" in n for n in names)

        release.set()
        await agent(
            action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
        )

    async def test_spawn_output_format_json_parses_summary(self, monkeypatch):
        from app.subagents import delegate as delegate_module
        from app.subagents.spawn import agent

        async def _fake_build(**_kwargs: Any) -> Any:
            return object()

        async def _fake_run(**_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "completed",
                "summary": '{"answer": "42"}',
                "telemetry": {},
            }

        monkeypatch.setattr(delegate_module, "build_child_agent", _fake_build)
        monkeypatch.setattr(delegate_module, "run_delegate", _fake_run)

        spawn = await agent(action="spawn", goal="x", output_format="json")
        out = await agent(
            action="result", task_id=spawn["task_id"], wait=True, timeout_s=5.0
        )
        assert out["result"]["summary"] == {"answer": "42"}
