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

"""Unit tests for the market research agent."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents import LlmAgent

from app.agent import root_agent
from app.sub_agents.competitor.agent import competitor_agent
from app.sub_agents.gap.agent import gap_agent
from app.sub_agents.location.agent import location_agent
from app.sub_agents.traffic.agent import traffic_agent
from app.tools.places import geocode_address, nearby_search

_TEST_LAT = 51.523
_TEST_LNG = -0.073
_PRICE_LEVEL_MODERATE = 2


def _tool_names(tools):
    """Tool names for a mix of plain functions and BaseTool instances.

    ADK stores a bare function passed to `tools=` as-is until it's wrapped
    at run time, so it only has `__name__`; AgentTool/BaseTool instances
    expose `.name` instead.
    """
    return [getattr(t, "name", getattr(t, "__name__", None)) for t in tools]


class TestAgentStructure:
    """Tests that the agent graph is wired correctly."""

    def test_root_agent_is_llm_agent(self):
        assert isinstance(root_agent, LlmAgent)

    def test_root_agent_has_required_tools(self):
        tool_names = _tool_names(root_agent.tools)
        assert "geocode_address" in tool_names
        assert "competitor_agent" in tool_names
        assert "location_agent" in tool_names
        assert "traffic_agent" in tool_names
        assert "gap_agent" in tool_names

    def test_sub_agents_are_llm_agents(self):
        for agent in [
            competitor_agent,
            location_agent,
            traffic_agent,
            gap_agent,
        ]:
            assert isinstance(agent, LlmAgent)

    def test_competitor_agent_tools(self):
        tool_names = _tool_names(competitor_agent.tools)
        assert "nearby_search" in tool_names
        assert "place_details" in tool_names
        assert "text_search" in tool_names

    def test_location_agent_tools(self):
        tool_names = _tool_names(location_agent.tools)
        assert "nearby_search" in tool_names
        assert "geocode_address" in tool_names

    def test_traffic_agent_tools(self):
        tool_names = _tool_names(traffic_agent.tools)
        assert "nearby_search" in tool_names
        assert "place_details" in tool_names

    def test_gap_agent_tools(self):
        tool_names = _tool_names(gap_agent.tools)
        assert "nearby_search" in tool_names
        assert "text_search" in tool_names
        assert "place_details" in tool_names


class TestPlacesTools:
    """Tests for the Google Places API tool functions."""

    @pytest.mark.asyncio
    async def test_geocode_address_returns_json(self):
        mock_response = {
            "status": "OK",
            "results": [
                {
                    "geometry": {
                        "location": {"lat": _TEST_LAT, "lng": _TEST_LNG}
                    },
                    "formatted_address": "15 Shoreditch High St, London",
                }
            ],
        }
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            # The response object itself is synchronous (httpx.Response.json()
            # and .raise_for_status() are not coroutines) — only the client's
            # get()/post() calls are async, so mock the response with
            # MagicMock rather than AsyncMock.
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_client.get.return_value = mock_response_obj

            result = await geocode_address("15 Shoreditch High St, London")
            data = json.loads(result)

            assert data["lat"] == _TEST_LAT
            assert data["lng"] == _TEST_LNG
            assert "formatted_address" in data

    @pytest.mark.asyncio
    async def test_geocode_address_handles_failure(self):
        mock_response = {"status": "ZERO_RESULTS", "results": []}
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_client.get.return_value = mock_response_obj

            result = await geocode_address("nonexistent place xyz")
            data = json.loads(result)

            assert "error" in data

    @pytest.mark.asyncio
    async def test_nearby_search_returns_json_list(self):
        mock_response = {
            "places": [
                {
                    "name": "places/abc123",
                    "id": "abc123",
                    "displayName": {"text": "Test Cafe"},
                    "formattedAddress": "1 Test St",
                    "rating": 4.2,
                    "userRatingCount": 150,
                    "priceLevel": "PRICE_LEVEL_MODERATE",
                    "location": {"latitude": _TEST_LAT, "longitude": _TEST_LNG},
                    "types": ["cafe"],
                }
            ]
        }
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_client.post.return_value = mock_response_obj

            result = await nearby_search(_TEST_LAT, _TEST_LNG, "cafe")
            places = json.loads(result)

            assert isinstance(places, list)
            assert len(places) == 1
            assert places[0]["name"] == "Test Cafe"
            assert places[0]["price_level"] == _PRICE_LEVEL_MODERATE

    @pytest.mark.asyncio
    async def test_nearby_search_normalizes_business_type(self):
        """business_type with spaces should be converted to underscores."""
        mock_response = {"places": []}
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            mock_response_obj = MagicMock()
            mock_response_obj.json.return_value = mock_response
            mock_client.post.return_value = mock_response_obj

            await nearby_search(_TEST_LAT, _TEST_LNG, "coffee shop")

            call_kwargs = mock_client.post.call_args
            body = call_kwargs[1]["json"]
            assert body["includedTypes"] == ["coffee_shop"]
