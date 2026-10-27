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

"""Market Research Agent — orchestrator and root agent definition."""

import os

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.tools.agent_tool import AgentTool

from .prompt import ORCHESTRATOR_PROMPT
from .sub_agents.competitor.agent import competitor_agent
from .sub_agents.gap.agent import gap_agent
from .sub_agents.location.agent import location_agent
from .sub_agents.traffic.agent import traffic_agent
from .tools.places import geocode_address

root_agent = LlmAgent(
    name="market_research_orchestrator",
    model=os.getenv("MODEL_NAME"),
    description=(
        "Performs comprehensive market research for any location and business "
        "type. Geocodes the address, runs competitor analysis, location "
        "scoring, traffic estimation, and gap analysis in parallel, then "
        "synthesizes the results into an actionable report."
    ),
    instruction=ORCHESTRATOR_PROMPT,
    tools=[
        geocode_address,
        AgentTool(agent=competitor_agent),
        AgentTool(agent=location_agent),
        AgentTool(agent=traffic_agent),
        AgentTool(agent=gap_agent),
    ],
)

app = App(root_agent=root_agent, name="app")
