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

"""``SandboxProcessHandle`` + ``SandboxEnvironment.spawn_process`` client.

Verifies the wire contract against the ``/processes/*`` endpoints with
``respx`` mocks. End-to-end behavior of the container side is covered
by ``test_runtime_processes.py``.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

LB_HOST = "abc123.us-central1.sandbox.aiplatform.googleapis.com"
BASE_URL = f"https://{LB_HOST}"
SANDBOX_NAME = (
    "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-1"
)


def _make_env(**overrides: Any) -> Any:
    from app.environment.sandbox import SandboxEnvironment

    kwargs: dict[str, Any] = {
        "client": MagicMock(),
        "sandbox_name": SANDBOX_NAME,
        "lb_host": LB_HOST,
        "routing_token": "route-token",
        "sandbox_token": "jwt-abc",
        "port": "8080",
        "owner": "alice",
        "root": Path("/workspace"),
    }
    kwargs.update(overrides)
    return SandboxEnvironment(**kwargs)


@pytest.mark.asyncio
@respx.mock
async def test_spawn_process_posts_command_returns_handle() -> None:
    env = _make_env()
    route = respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_abc", "pid": 4242}
        )
    )

    handle = await env.spawn_process("sleep 5")

    assert route.called
    posted = route.calls.last.request.read().decode()
    assert "sleep 5" in posted
    assert handle.session_id == "proc_abc"
    assert handle.pid == 4242
    assert handle.is_running is True


@pytest.mark.asyncio
@respx.mock
async def test_spawn_process_forwards_cwd_and_env() -> None:
    env = _make_env()
    route = respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_xx", "pid": 1}
        )
    )

    await env.spawn_process("ls", cwd="/workspace/sub", env={"FOO": "bar"})

    body = route.calls.last.request.read().decode()
    assert "/workspace/sub" in body
    assert "FOO" in body


@pytest.mark.asyncio
@respx.mock
async def test_handle_read_decodes_base64_and_tracks_exit_code() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("echo hi")

    payload = base64.b64encode(b"hello\n").decode("ascii")
    respx.get(f"{BASE_URL}/processes/proc_x/output").mock(
        return_value=httpx.Response(
            200, json={"data_b64": payload, "next_offset": 6, "exit_code": 0}
        )
    )

    data, next_offset, exit_code = await handle.read(offset=0)
    assert data == b"hello\n"
    assert next_offset == 6
    assert exit_code == 0
    assert handle.is_running is False
    assert handle.exit_code == 0


@pytest.mark.asyncio
@respx.mock
async def test_handle_read_pagination_passes_offset_limit() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("echo hi")

    route = respx.get(f"{BASE_URL}/processes/proc_x/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "data_b64": base64.b64encode(b"abc").decode("ascii"),
                "next_offset": 13,
                "exit_code": None,
            },
        )
    )

    await handle.read(offset=10, limit=3)

    req = route.calls.last.request
    assert req.url.params["offset"] == "10"
    assert req.url.params["limit"] == "3"


@pytest.mark.asyncio
@respx.mock
async def test_handle_write_base64_encodes_and_submits() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("read x")

    route = respx.post(f"{BASE_URL}/processes/proc_x/stdin").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    await handle.write(b"hi\n")

    body = route.calls.last.request.read().decode()
    assert base64.b64encode(b"hi\n").decode("ascii") in body
    assert '"submit": false' in body or '"submit":false' in body


@pytest.mark.asyncio
@respx.mock
async def test_handle_write_after_finished_raises() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("true")

    respx.get(f"{BASE_URL}/processes/proc_x/output").mock(
        return_value=httpx.Response(
            200,
            json={
                "data_b64": base64.b64encode(b"").decode("ascii"),
                "next_offset": 0,
                "exit_code": 0,
            },
        )
    )
    await handle.read()
    assert handle.is_running is False

    with pytest.raises(RuntimeError):
        await handle.write(b"x")


@pytest.mark.asyncio
@respx.mock
async def test_handle_kill_marks_finished_and_records_exit_code() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("sleep 30")

    respx.post(f"{BASE_URL}/processes/proc_x/kill").mock(
        return_value=httpx.Response(200, json={"exit_code": -15})
    )

    await handle.kill()
    assert handle.is_running is False
    assert handle.exit_code == -15


@pytest.mark.asyncio
@respx.mock
async def test_handle_wait_polls_until_finished() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("sleep 0")

    responses = [
        httpx.Response(
            200,
            json={
                "session_id": "proc_x",
                "pid": 1,
                "command": "sleep 0",
                "is_running": True,
                "exit_code": None,
                "output_size": 0,
                "idle_seconds": 0.0,
            },
        ),
        httpx.Response(
            200,
            json={
                "session_id": "proc_x",
                "pid": 1,
                "command": "sleep 0",
                "is_running": False,
                "exit_code": 0,
                "output_size": 5,
                "idle_seconds": 0.1,
            },
        ),
    ]
    respx.get(f"{BASE_URL}/processes/proc_x/status").mock(side_effect=responses)

    exit_code = await handle.wait(timeout=5.0)
    assert exit_code == 0
    assert handle.is_running is False


@pytest.mark.asyncio
@respx.mock
async def test_handle_refresh_status_updates_output_size() -> None:
    env = _make_env()
    respx.post(f"{BASE_URL}/processes").mock(
        return_value=httpx.Response(
            200, json={"session_id": "proc_x", "pid": 1}
        )
    )
    handle = await env.spawn_process("yes")

    respx.get(f"{BASE_URL}/processes/proc_x/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": "proc_x",
                "pid": 1,
                "command": "yes",
                "is_running": True,
                "exit_code": None,
                "output_size": 2048,
                "idle_seconds": 0.2,
            },
        )
    )

    await handle.refresh_status()
    assert handle.output_size == 2048
