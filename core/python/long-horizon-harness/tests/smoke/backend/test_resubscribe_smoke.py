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

"""HTTP-level: tasks/resubscribe for an unknown task must NOT return an empty
200 SSE stream (the bug: client hangs). It must carry a JSON-RPC error — either
as a single SSE error frame in a 200 text/event-stream body, or as a
non-streaming JSON-RPC error response. Either is acceptable; an empty 200 is
not.

On a2a 1.x the v0.3 JSONRPCAdapter turns a mid-stream raise into an SSE error
frame upstream (the old ResilientRequestHandler override is gone); this guards
that the wiring keeps that behavior end-to-end.

Gated by RUN_SMOKE=1 (hits FastAPI, no LLM).
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.agents import Agent

from horizon.a2a.routes import attach_a2a_routes

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SMOKE") != "1", reason="RUN_SMOKE=1 required"
)


class _NoopExecutor:
    async def execute(
        self, *_a, **_k
    ):  # pragma: no cover - resubscribe never executes
        return None

    async def cancel(self, *_a, **_k):  # pragma: no cover
        return None


@pytest_asyncio.fixture
async def smoke_client() -> TestClient:
    # Minimal app that wires the real A2A JSON-RPC routes over an in-memory task
    # store. This exercises the exact SSE-serialization path that the bug
    # crashed on, without standing up the full lifespan (which needs Vertex /
    # sandbox).
    app = FastAPI()
    agent = Agent(
        name="smoke_agent", model="gemini-3.7-flash", description="smoke"
    )
    await attach_a2a_routes(
        app,
        agent=agent,
        agent_executor=_NoopExecutor(),
        task_store=InMemoryTaskStore(),
    )
    return TestClient(app)


def _resubscribe_body(task_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "smoke-resub-1",
        "method": "tasks/resubscribe",
        "params": {"id": task_id},
    }


def test_resubscribe_unknown_task_carries_error(
    smoke_client: TestClient,
) -> None:
    resp = smoke_client.post("/a2a", json=_resubscribe_body("does-not-exist"))
    assert resp.status_code == 200
    body = resp.text
    assert body.strip(), "empty 200 body — client would hang"
    found_error = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:") :].strip())
        if isinstance(payload, dict) and "error" in payload:
            found_error = True
            break
    assert found_error, f"no error frame in resubscribe stream: {body!r}"
