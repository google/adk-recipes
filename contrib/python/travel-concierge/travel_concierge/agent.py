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

"""Demonstration of Travel AI Conceirge using Agent Development Kit"""

import uuid

from google.adk.agents import Agent
from openinference.instrumentation import using_session

from travel_concierge import prompt
from travel_concierge.sub_agents.booking.agent import booking_agent
from travel_concierge.sub_agents.in_trip.agent import in_trip_agent
from travel_concierge.sub_agents.inspiration.agent import inspiration_agent
from travel_concierge.sub_agents.planning.agent import planning_agent
from travel_concierge.sub_agents.post_trip.agent import post_trip_agent
from travel_concierge.sub_agents.pre_trip.agent import pre_trip_agent
from travel_concierge.tools.memory import _load_precreated_itinerary
from travel_concierge.tracing import instrument_adk_with_arize

from . import MODEL

_ = instrument_adk_with_arize()


with using_session(session_id=uuid.uuid4()):
    root_agent = Agent(
        model=MODEL,
        name="root_agent",
        description="A Travel Conceirge using the services of multiple sub-agents",
        instruction=prompt.ROOT_AGENT_INSTR,
        sub_agents=[
            inspiration_agent,
            planning_agent,
            booking_agent,
            pre_trip_agent,
            in_trip_agent,
            post_trip_agent,
        ],
        before_agent_callback=_load_precreated_itinerary,
    )
