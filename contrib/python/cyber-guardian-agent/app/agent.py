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

import logging
import os

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.planners import BuiltInPlanner
from google.genai import types

from .prompt import (
    root_agent_instruction,
)  # Instruction for the orch_agent LLM persona
from .sub_agents.investigation.agent import investigation_agent
from .sub_agents.response.agent import response_agent
from .sub_agents.threatintel.agent import threatintel_agent
from .sub_agents.triage.agent import triage_agent

# --- Configure Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

root_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="cyber_guardian_orchestrator",
    description="Orchestrates a multi-agent cybersecurity incident response workflow",
    instruction=root_agent_instruction,
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            include_thoughts=True, thinking_budget=512
        )
    ),
    sub_agents=[
        threatintel_agent,
        investigation_agent,
        triage_agent,
        response_agent,
    ],
)

app = App(root_agent=root_agent, name="cyber_guardian")
