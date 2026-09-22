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

"""Cross-border data-policy router — orchestrator and root agent definition.

Evaluates a data-processing request against a registry of regional Agent
Cards (`app/policy/cards.py`) using a data-residency/jurisdiction policy
engine (`app/policy/engine.py`), then delegates to whichever regional
processor sub-agent the policy approved.
"""

import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from .prompt import ORCHESTRATOR_INSTRUCTION
from .sub_agents.eu_processor.agent import eu_processor_agent
from .sub_agents.uk_processor.agent import uk_processor_agent
from .sub_agents.us_processor.agent import us_processor_agent
from .tools.routing_tool import evaluate_and_route


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "Routes data-processing requests to a jurisdiction-compliant "
            "regional agent by evaluating cross-border data-residency "
            "policy against each candidate agent's Agent Card."
        ),
        instruction=ORCHESTRATOR_INSTRUCTION,
        tools=[
            evaluate_and_route,
            AgentTool(agent=eu_processor_agent),
            AgentTool(agent=uk_processor_agent),
            AgentTool(agent=us_processor_agent),
        ],
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
