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

"""Unit tests for ``horizon.a2a.routes.attach_a2a_routes``.

The function is pure plumbing: build an agent card, register A2A JSON-RPC +
well-known card routes. These tests cover route registration and that the
card's URL/version come from the environment.
"""

from __future__ import annotations

import pytest
from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.agents.llm_agent import LlmAgent

from horizon.a2a.routes import A2A_RPC_PATH, attach_a2a_routes


def _make_agent() -> LlmAgent:
    return LlmAgent(
        name="test_agent",
        model="gemini-3.7-flash",
        description="A test agent",
        instruction="Test.",
    )


class _NoopExecutor:
    async def execute(self, *_a, **_k):
        return None

    async def cancel(self, *_a, **_k):
        return None


def _attach(app: FastAPI):
    return attach_a2a_routes(
        app,
        agent=_make_agent(),
        agent_executor=_NoopExecutor(),
        task_store=InMemoryTaskStore(),
    )


@pytest.mark.asyncio
async def test_registers_rpc_and_card_routes() -> None:
    app = FastAPI()
    pre = {r.path for r in app.router.routes if hasattr(r, "path")}

    await _attach(app)

    new = {r.path for r in app.router.routes if hasattr(r, "path")} - pre
    assert A2A_RPC_PATH in new
    assert "/.well-known/agent-card.json" in new


@pytest.mark.asyncio
async def test_card_streams_and_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_URL", "https://example.test")
    monkeypatch.setenv("AGENT_VERSION", "9.9.9")

    app = FastAPI()
    await _attach(app)

    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()

    assert card["url"] == f"https://example.test{A2A_RPC_PATH}"
    assert card["version"] == "9.9.9"
    assert card["name"] == "test_agent"
    assert card["capabilities"]["streaming"] is True
