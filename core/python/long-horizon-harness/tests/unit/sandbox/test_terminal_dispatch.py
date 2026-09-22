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

"""``process(action='spawn')`` dispatches to ``SandboxProcessHandle``
when the active env is ``SandboxEnvironment``. Mocks the runtime shim
with ``respx`` — no real container or subprocess.

Spawn used to be ``bash(command=..., background=True)``; it moved to
``process(action='spawn')`` when the parameter space was minimized
(bash keeps only ``command``/``timeout_s``). Both paths
share the same ``open_handle`` under ``_spawn.py``, so the assertions
below are unchanged, only the call site is.
"""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from horizon.environment_context import (
    clear_active_environment,
    set_active_environment,
)

LB_HOST = "lb.example.com"
BASE_URL = f"https://{LB_HOST}"


def _make_sandbox_env(root: Path) -> Any:
    from horizon.environment.sandbox import SandboxEnvironment

    return SandboxEnvironment(
        client=MagicMock(),
        sandbox_name="sb",
        lb_host=LB_HOST,
        routing_token="rt",
        sandbox_token="jwt",
        port="8080",
        owner="alice",
        root=root,
    )


def _ctx(state: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=state if state is not None else {},
        tool_confirmation=None,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=lambda **_: None,
    )


@pytest.fixture
def sandbox_env(tmp_path: Path):
    env = _make_sandbox_env(root=tmp_path)
    set_active_environment(env)
    try:
        yield env
    finally:
        clear_active_environment()


@pytest.mark.asyncio
@respx.mock
async def test_background_spawn_calls_runtime_processes_endpoint(
    sandbox_env,
) -> None:
    from horizon.tools.processes.process import process

    route = respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_sand1", "pid": 9999}
        )
    )

    result = await process(
        action="spawn", command="sleep 30", tool_context=_ctx()
    )

    assert route.called
    assert result["session_id"] == "proc_sand1"
    assert result["pid"] == 9999
    assert result["status"] == "running"


@pytest.mark.asyncio
@respx.mock
async def test_background_spawn_registers_handle_for_process_tool(
    sandbox_env,
) -> None:
    from horizon.tools.processes.process import process

    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_sand2", "pid": 1}
        )
    )

    ctx = _ctx()
    await process(action="spawn", command="sleep 30", tool_context=ctx)
    listing = await process(action="list", tool_context=ctx)
    running_ids = [s["session_id"] for s in listing["running"]]
    assert "proc_sand2" in running_ids


@pytest.mark.asyncio
@respx.mock
async def test_process_tool_kill_routes_to_runtime(sandbox_env) -> None:
    from horizon.tools.processes.process import process

    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_sand3", "pid": 1}
        )
    )
    kill_route = respx.post(f"{BASE_URL}/processes/proc_sand3/kill").mock(
        return_value=httpx.Response(200, json={"exit_code": -15})
    )

    ctx = _ctx()
    await process(action="spawn", command="sleep 30", tool_context=ctx)
    result = await process(
        action="kill", session_id="proc_sand3", tool_context=ctx
    )

    assert kill_route.called
    assert result["status"] == "killed"
    assert result["exit_code"] == -15


@pytest.mark.asyncio
@respx.mock
async def test_process_tool_log_decodes_remote_output(sandbox_env) -> None:
    from horizon.tools.processes.process import process

    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_sand4", "pid": 1}
        )
    )
    payload = base64.b64encode(b"line-from-sandbox\n").decode("ascii")
    respx.get(f"{BASE_URL}/processes/proc_sand4/output").mock(
        return_value=httpx.Response(
            200,
            json={"data_b64": payload, "next_offset": 18, "exit_code": 0},
        )
    )

    ctx = _ctx()
    await process(
        action="spawn", command="echo line-from-sandbox", tool_context=ctx
    )
    log = await process(action="log", session_id="proc_sand4", tool_context=ctx)

    assert "line-from-sandbox" in log["data"]
    assert log["exit_code"] == 0
