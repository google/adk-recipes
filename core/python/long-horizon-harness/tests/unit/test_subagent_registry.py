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

"""``SubAgentRegistry`` tracks fire-and-forget child agents.

The registry is the substrate behind the ``agent(action=...)`` dispatch
tool — ``delegate`` stays as a thin wrapper that spawns + waits.

Tests stub ``run_delegate`` so no real Agent/Runner is constructed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# =============================================================================
# Registry surface
# =============================================================================


async def test_module_exposes_entrypoints():
    from app.subagents import registry

    assert callable(registry.get_registry)
    assert hasattr(registry, "SubAgentRegistry")
    assert hasattr(registry, "SubAgentHandle")


async def test_register_returns_unique_task_ids():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()

    async def _noop() -> dict[str, Any]:
        return {"status": "completed", "summary": "", "telemetry": {}}

    task_a = await reg.register(coro_factory=_noop, goal="a")
    task_b = await reg.register(coro_factory=_noop, goal="b")
    assert task_a != task_b
    statuses = {task_a, task_b}
    assert len(statuses) == 2


async def test_get_status_returns_unknown_for_missing_task():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    result = await reg.get_status("nonexistent")
    assert result["status"] == "unknown"


async def test_pending_then_completed_lifecycle():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow() -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"status": "completed", "summary": "done", "telemetry": {}}

    task_id = await reg.register(coro_factory=_slow, goal="slow")
    await started.wait()

    status = await reg.get_status(task_id)
    assert status["status"] in {"pending", "running"}

    release.set()
    result = await reg.get_result(task_id, wait=True, timeout_s=5.0)
    assert result["status"] == "completed"
    assert result["result"]["summary"] == "done"

    final = await reg.get_status(task_id)
    assert final["status"] == "completed"


async def test_get_result_without_wait_returns_pending():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    release = asyncio.Event()

    async def _slow() -> dict[str, Any]:
        await release.wait()
        return {"status": "completed", "summary": "ok", "telemetry": {}}

    task_id = await reg.register(coro_factory=_slow, goal="x")
    out = await reg.get_result(task_id, wait=False)
    assert out["status"] in {"pending", "running"}
    release.set()
    # Drain
    await reg.get_result(task_id, wait=True, timeout_s=5.0)


async def test_cancel_marks_handle_cancelled():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _runs_forever() -> dict[str, Any]:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise
        return {"status": "completed", "summary": "", "telemetry": {}}

    task_id = await reg.register(coro_factory=_runs_forever, goal="forever")
    await started.wait()
    cancelled = await reg.cancel(task_id)
    assert cancelled is True

    # Give the loop a tick to process cancellation.
    for _ in range(50):
        status = await reg.get_status(task_id)
        if status["status"] == "cancelled":
            break
        await asyncio.sleep(0.01)
    assert status["status"] == "cancelled"


async def test_failed_task_surfaces_error():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()

    async def _boom() -> dict[str, Any]:
        raise RuntimeError("kaboom")

    task_id = await reg.register(coro_factory=_boom, goal="boom")
    result = await reg.get_result(task_id, wait=True, timeout_s=5.0)
    assert result["status"] == "failed"
    assert "kaboom" in result.get("error", "")


async def test_list_active_includes_pending_and_running_only():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    release_a = asyncio.Event()

    async def _slow() -> dict[str, Any]:
        await release_a.wait()
        return {"status": "completed", "summary": "", "telemetry": {}}

    async def _quick() -> dict[str, Any]:
        return {"status": "completed", "summary": "", "telemetry": {}}

    a = await reg.register(coro_factory=_slow, goal="slow")
    b = await reg.register(coro_factory=_quick, goal="quick")

    await reg.get_result(b, wait=True, timeout_s=5.0)
    active = await reg.list_active()
    ids = {h["task_id"] for h in active}
    assert a in ids
    assert b not in ids

    release_a.set()
    await reg.get_result(a, wait=True, timeout_s=5.0)


# =============================================================================
# wait() — fleet next-completed primitive
# =============================================================================


async def test_wait_returns_first_completed_with_result():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    release_slow = asyncio.Event()

    async def _fast() -> dict[str, Any]:
        return {"status": "completed", "summary": "fast-done", "telemetry": {}}

    async def _slow() -> dict[str, Any]:
        await release_slow.wait()
        return {"status": "completed", "summary": "slow", "telemetry": {}}

    fast = await reg.register(coro_factory=_fast, goal="fast")
    slow = await reg.register(coro_factory=_slow, goal="slow")

    out = await reg.wait(task_ids=None, timeout_s=5.0)
    assert out["task_id"] == fast
    assert out["status"] == "completed"
    assert out["result"]["summary"] == "fast-done"
    assert slow in out["still_running"]

    release_slow.set()
    await reg.get_result(slow, wait=True, timeout_s=5.0)


async def test_wait_no_active_tasks():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    out = await reg.wait(task_ids=None, timeout_s=1.0)
    assert out["status"] == "no_active_tasks"


async def test_wait_timeout_returns_still_running():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    release = asyncio.Event()

    async def _slow() -> dict[str, Any]:
        await release.wait()
        return {"status": "completed", "summary": "", "telemetry": {}}

    slow = await reg.register(coro_factory=_slow, goal="slow")
    out = await reg.wait(task_ids=None, timeout_s=0.05)
    assert out["status"] == "timeout"
    assert slow in out["still_running"]

    release.set()
    await reg.get_result(slow, wait=True, timeout_s=5.0)


async def test_wait_scoped_to_given_task_ids():
    from app.subagents.registry import SubAgentRegistry

    reg = SubAgentRegistry()
    release_a = asyncio.Event()

    async def _slow_a() -> dict[str, Any]:
        await release_a.wait()
        return {"status": "completed", "summary": "a", "telemetry": {}}

    async def _fast_b() -> dict[str, Any]:
        return {"status": "completed", "summary": "b", "telemetry": {}}

    a = await reg.register(coro_factory=_slow_a, goal="a")
    b = await reg.register(coro_factory=_fast_b, goal="b")
    await reg.get_result(b, wait=True, timeout_s=5.0)

    # Scope wait to the still-running `a` only; it must time out (b is done).
    out = await reg.wait(task_ids=[a], timeout_s=0.05)
    assert out["status"] == "timeout"

    release_a.set()
    await reg.get_result(a, wait=True, timeout_s=5.0)


async def test_get_registry_returns_same_instance_per_context():
    from app.subagents.registry import get_registry

    first = get_registry()
    second = get_registry()
    assert first is second


async def test_get_registry_survives_across_copied_contexts():
    """Each ADK tool callback runs in its own copied ``contextvars.Context``.

    The registry must outlive a single tool invocation — otherwise a handle
    stored during ``agent(action='spawn')`` becomes unreachable from the next
    ``agent(action='result')`` call.
    """
    import contextvars

    from app.subagents.registry import (
        get_registry,
        reset_registry,
    )

    reset_registry()

    def _capture(box: list) -> None:
        box.append(get_registry())

    first_box: list = []
    second_box: list = []
    contextvars.copy_context().run(_capture, first_box)
    contextvars.copy_context().run(_capture, second_box)

    assert first_box[0] is second_box[0]
