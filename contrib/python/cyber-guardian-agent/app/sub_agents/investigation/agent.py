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

from ...tools import investigationQueryTool
from .prompt import agent_instructions

investigation_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="investigation_agent",
    description="Performs incident investigation using internal DBs and sandboxes",
    instruction=agent_instructions,
    tools=[FunctionTool(investigationQueryTool)],
    output_key="investigation_agent_output",
)
