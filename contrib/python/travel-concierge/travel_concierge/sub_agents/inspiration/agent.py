# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Inspiration agent. A pre-booking agent covering the ideation part of the trip."""

import logging

from google.adk.agents import Agent
from google.adk.tools.agent_tool import AgentTool

from travel_concierge import MODEL
from travel_concierge.shared_libraries.types import (
    DestinationIdeas,
    POISuggestions,
    json_response_config,
)
from travel_concierge.sub_agents.inspiration import prompt
from travel_concierge.tools.places import get_places_toolset

place_agent = Agent(
    model=MODEL,
    name="place_agent",
    instruction=prompt.PLACE_AGENT_INSTR,
    description="This agent suggests a few destination given some user preferences",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_schema=DestinationIdeas,
    output_key="place",
    generate_content_config=json_response_config,
)

maps_grounding_toolset = []
try:
    maps_grounding_toolset = [get_places_toolset()]
except OSError:
    logging.warning(
        "Google Maps Grounding Lite tool is not available. Check if GOOGLE_MAPS_API_KEY is set."
    )

poi_agent = Agent(
    model=MODEL,
    name="poi_agent",
    description="This agent suggests a few activities and points of interests given a destination",
    instruction=prompt.POI_AGENT_INSTR,
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    output_schema=POISuggestions,
    output_key="poi",
    generate_content_config=json_response_config,
    tools=maps_grounding_toolset,
)

inspiration_agent = Agent(
    model=MODEL,
    name="inspiration_agent",
    description="A travel inspiration agent who inspire users, and discover their next vacations; Provide information about places, activities, interests,",
    instruction=prompt.INSPIRATION_AGENT_INSTR,
    tools=[AgentTool(agent=place_agent), AgentTool(agent=poi_agent)],
)
