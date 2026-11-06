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

from ...tools import threatIntelQueryTool
from .prompt import agent_instructions

threatintel_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="threat_intel_agent",
    description="Enriches IPs/domains/hashes with threat intelligence context",
    instruction=agent_instructions,
    tools=[FunctionTool(threatIntelQueryTool)],
    output_key="threatintel_agent_output",
)
