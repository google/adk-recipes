# Copyright 2025 Google LLC
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

from typing import Any

from google.adk.agents import Agent

from .prompt import agent_instruction
from .tools.tools import (
    get_current_date,
    langchain_tool,
    mcp_tools,
    search_tool,
    toolbox_tools,
)

# Build tools list, filtering out empty/None values
tools: list[Any] = [get_current_date, search_tool, langchain_tool]
if toolbox_tools:  # Only add if not empty list
    tools.extend(toolbox_tools)
if mcp_tools is not None:  # Only add if not None
    tools.append(mcp_tools)

root_agent = Agent(
    model="gemini-3.5-flash",
    name="software_bug_assistant",
    instruction=agent_instruction,
    tools=tools,
)
