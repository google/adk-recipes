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

"""The served A2A card must stay consumable by v0.3 clients (Gemini Enterprise).

a2a 1.x serves a 1.0 card (interfaces nested in ``supportedInterfaces``). GE's
Discovery Engine validator still requires the 0.3 shape (top-level ``url`` +
``protocolVersion``). ``attach_a2a_routes`` advertises an extra v0.3
``AgentInterface`` via ``card_modifier``, which makes a2a-sdk also emit the 0.3
top-level fields — so modern 1.0 clients and GE both work off one endpoint.
"""

from __future__ import annotations

import asyncio

from a2a.server.tasks import InMemoryTaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.agents import Agent

from horizon.a2a.routes import attach_a2a_routes


class _NoopExecutor:
    async def execute(
        self, *_a, **_k
    ):  # pragma: no cover - card test never executes
        return None

    async def cancel(self, *_a, **_k):  # pragma: no cover
        return None


def _card() -> dict:
    app = FastAPI()
    agent = Agent(
        name="card_agent", model="gemini-3.7-flash", description="card test"
    )
    asyncio.run(
        attach_a2a_routes(
            app,
            agent=agent,
            agent_executor=_NoopExecutor(),
            task_store=InMemoryTaskStore(),
        )
    )
    resp = TestClient(app).get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    return resp.json()


def test_card_advertises_both_1_0_and_0_3_interfaces() -> None:
    card = _card()
    versions = {i["protocolVersion"] for i in card["supportedInterfaces"]}
    assert versions == {"1.0", "0.3"}


def test_card_carries_v0_3_top_level_fields_for_ge() -> None:
    card = _card()
    # GE's Discovery Engine validator rejects a pure-1.0 card with
    # "required property 'protocolVersion' not found".
    assert card["protocolVersion"] == "0.3"
    assert card["url"]


def test_card_modifier_is_idempotent_across_fetches() -> None:
    # card_modifier mutates the shared card object on every GET; without a dedup
    # guard the extra 0.3 interface accumulates on each fetch.
    app = FastAPI()
    agent = Agent(
        name="card_agent", model="gemini-3.7-flash", description="card test"
    )
    asyncio.run(
        attach_a2a_routes(
            app,
            agent=agent,
            agent_executor=_NoopExecutor(),
            task_store=InMemoryTaskStore(),
        )
    )
    client = TestClient(app)
    client.get("/.well-known/agent-card.json")
    card = client.get("/.well-known/agent-card.json").json()
    v03 = [
        i for i in card["supportedInterfaces"] if i["protocolVersion"] == "0.3"
    ]
    assert len(v03) == 1, card["supportedInterfaces"]
