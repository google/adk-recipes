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

"""The root-agent gemini backend requests Vertex's priority service tier."""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def test_gemini_entry_is_on_demand_by_default():
    # The priority tier is opt-in (LHA_VERTEX_SERVICE_TIER=priority); the default
    # registry entry is a plain Gemini so projects without the entitlement aren't
    # force-pinned. See test_registry_lazy.py for the opt-in path.
    from google.adk.models import Gemini

    from horizon.models.registry import MODEL_REGISTRY, _PriorityGemini

    entry = MODEL_REGISTRY["gemini-3.7-flash"]
    assert isinstance(entry, Gemini)
    assert not isinstance(entry, _PriorityGemini)


def test_priority_tier_literal_round_trips_through_sdk():
    # Pins the google-genai _missing_ fallback we depend on: the SDK enum has no
    # SERVICE_TIER_* member, so the Vertex literal must survive as its own value.
    # Fails loudly if an SDK upgrade changes that behavior.
    from horizon.models.registry import _PRIORITY_SERVICE_TIER

    assert _PRIORITY_SERVICE_TIER is not None
    assert _PRIORITY_SERVICE_TIER.value == "SERVICE_TIER_PRIORITY"


@pytest.mark.asyncio
async def test_priority_gemini_sets_service_tier(monkeypatch):
    from horizon.models.registry import _PriorityGemini

    seen: dict[str, Any] = {}

    async def fake_super(self, llm_request: LlmRequest, stream: bool = False):
        seen["tier"] = llm_request.config.service_tier
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="ok")])
        )

    # Patch the parent so super() delegation is captured without hitting Vertex.
    monkeypatch.setattr(Gemini, "generate_content_async", fake_super)

    g = _PriorityGemini(model="gemini-3.7-flash")
    req = LlmRequest(model="gemini-3.7-flash")
    _ = [r async for r in g.generate_content_async(req)]

    assert seen["tier"] is not None
    assert seen["tier"].value == "SERVICE_TIER_PRIORITY"
