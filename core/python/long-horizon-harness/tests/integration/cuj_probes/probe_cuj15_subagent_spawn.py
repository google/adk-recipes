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

"""CUJ-15: sub-agent spawn lifecycle — happy path, cancel, failure, structural recursion guard.

Bypasses the LLM-driven child agent: each test installs its own coroutine
factory in the registry directly via ``register``. The lifecycle surface
(``agent(action='status'|'result'|'cancel'|'list')``) is the production
code under test — only the work the child *does* is faked.

Recursion guard is structural: the toolsets a delegate child can receive
(``file`` / ``shell`` / ``web``) do not include the ``agent``
dispatch tool, so a child cannot fan out further regardless of what its
LLM tries. This probe asserts that property directly so a future toolset
edit can't silently re-enable grandchildren.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def registry():
    from app.subagents.registry import SubAgentRegistry

    return SubAgentRegistry()


async def test_spawn_status_and_result_happy_path(registry):
    async def factory():
        await asyncio.sleep(0.02)
        return {"success": True, "summary": "did the thing"}

    task_id = await registry.register(coro_factory=factory, goal="trivial task")

    status = await registry.get_status(task_id)
    assert status["task_id"] == task_id
    assert status["status"] in {"running", "completed"}

    result = await registry.get_result(task_id, wait=True, timeout_s=2.0)
    assert result["status"] == "completed"
    assert result["result"] == {"success": True, "summary": "did the thing"}

    final = await registry.get_status(task_id)
    assert final["status"] == "completed"


async def test_cancel_mid_flight_returns_cancelled(registry):
    started = asyncio.Event()
    never_finishes = asyncio.Event()  # never set → child blocks forever

    async def slow_factory():
        started.set()
        await never_finishes.wait()
        return {"success": True}

    task_id = await registry.register(
        coro_factory=slow_factory, goal="never-completes"
    )

    await asyncio.wait_for(started.wait(), timeout=1.0)

    cancelled_result = await registry.cancel(task_id)
    assert cancelled_result is True

    await asyncio.sleep(0.01)

    status = await registry.get_status(task_id)
    assert status["status"] == "cancelled"

    result = await registry.get_result(task_id, wait=False)
    assert result["status"] == "cancelled"


async def test_child_failure_surfaces_as_failed(registry):
    async def boom():
        raise RuntimeError("downstream API timed out")

    task_id = await registry.register(coro_factory=boom, goal="will fail")

    result = await registry.get_result(task_id, wait=True, timeout_s=1.0)
    assert result["status"] == "failed"
    assert "downstream API timed out" in result["error"]


async def test_get_result_timeout_does_not_kill_task(registry):
    keep_running = asyncio.Event()

    async def slow():
        await keep_running.wait()
        return {"success": True}

    task_id = await registry.register(coro_factory=slow, goal="slow")

    timed_out = await registry.get_result(task_id, wait=True, timeout_s=0.05)
    assert timed_out["status"] == "timeout"

    keep_running.set()
    final = await registry.get_result(task_id, wait=True, timeout_s=1.0)
    assert final["status"] == "completed"


async def test_parent_cancel_cascades_to_children(registry):
    parent_started = asyncio.Event()
    child_started = asyncio.Event()
    block_forever = asyncio.Event()

    async def parent_factory():
        parent_started.set()
        await block_forever.wait()
        return {"success": True}

    async def child_factory():
        child_started.set()
        await block_forever.wait()
        return {"success": True}

    parent_id = await registry.register(
        coro_factory=parent_factory, goal="parent"
    )
    child_id = await registry.register(
        coro_factory=child_factory, goal="child", parent_id=parent_id
    )

    await asyncio.wait_for(parent_started.wait(), timeout=1.0)
    await asyncio.wait_for(child_started.wait(), timeout=1.0)

    await registry.cancel(parent_id)
    await asyncio.sleep(0.01)

    assert (await registry.get_status(parent_id))["status"] == "cancelled"
    assert (await registry.get_status(child_id))["status"] == "cancelled", (
        "cancelling the parent must cascade to its registered children"
    )


async def test_child_toolsets_do_not_include_agent_dispatch():
    """Structural recursion guard: the agent() dispatch tool is not in any
    delegate toolset.

    If this ever fails, a delegate child could spawn grandchildren and
    the registry's cancellation cascade no longer matches what the
    parent agent reasoning expects.
    """
    from app.subagents import spawn as spawn_module
    from app.subagents.toolsets import TOOLSETS

    forbidden = {spawn_module.agent}

    for toolset_name, tools in TOOLSETS.items():
        overlap = forbidden.intersection(tools)
        assert not overlap, (
            f"toolset {toolset_name!r} unexpectedly exposes the agent() "
            f"lifecycle dispatch to delegate children: {overlap}"
        )
