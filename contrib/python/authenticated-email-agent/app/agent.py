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

"""Google ADK agent backed by e2a's hosted MCP email tools."""

import os

from google.adk.agents import Agent
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from .prompt import EMAIL_AGENT_INSTRUCTION

E2A_MCP_URL = "https://api.e2a.dev/mcp"
E2A_TOOL_FILTER = (
    "whoami",
    "list_messages",
    "get_message",
    "send_message",
    "reply_to_message",
)


def _authorization_headers() -> dict[str, str]:
    """Build the bearer header, failing closed when the key is missing."""
    return {"Authorization": f"Bearer {os.environ['E2A_API_KEY']}"}


def _email_toolset() -> McpToolset:
    """Connect ADK to the agent-scoped e2a MCP endpoint."""
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=E2A_MCP_URL,
            headers=_authorization_headers(),
            timeout=30,
        ),
        tool_filter=list(E2A_TOOL_FILTER),
    )


root_agent = Agent(
    name="authenticated_email_agent",
    model=os.environ["MODEL_NAME"],
    description=(
        "Reads and drafts authenticated email through an agent-scoped e2a "
        "inbox."
    ),
    instruction=EMAIL_AGENT_INSTRUCTION,
    tools=[_email_toolset()],
)
