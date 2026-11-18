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

import os

from google.adk.agents import Agent
from google.adk.tools import FunctionTool

from ...tools import triageQueryTool
from .prompt import agent_instructions

triage_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="triage_agent",
    description="Assesses alert severity, deduplication, and context via SIEM",
    instruction=agent_instructions,
    tools=[FunctionTool(triageQueryTool)],
    output_key="triage_agent_output",
)
