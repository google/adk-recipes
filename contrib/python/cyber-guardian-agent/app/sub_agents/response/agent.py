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

from ...tools import getPlaybookTool, responseExecutionTool
from .prompt import agent_instructions

response_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="response_agent",
    description="Recommends and triggers incident response actions",
    instruction=agent_instructions,
    tools=[FunctionTool(responseExecutionTool), FunctionTool(getPlaybookTool)],
    output_key="response_agent_output",
)
