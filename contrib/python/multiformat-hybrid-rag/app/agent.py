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
import sys

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.mcp_tool.mcp_toolset import (
    McpToolset,
    SseConnectionParams,
    StdioConnectionParams,
)
from google.genai import types
from mcp import StdioServerParameters

from app.config import AGENT_MODEL, MODEL_LOCATION

LLM = AGENT_MODEL
# Model endpoint, not the data region. Previously this guessed from the
# model name ("global" if "preview" in LLM), which silently 404s on the
# gemini-3.x family: those are global-only but carry no "preview" suffix.
LLM_LOCATION = MODEL_LOCATION
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL")

# Seconds to wait for the MCP server to become reachable. Generous because
# the stdio transport pays a cold Python-subprocess start, and the SSE
# transport can hit a Cloud Run scale-from-zero.
MCP_CONNECTION_TIMEOUT = 120

# In-process environment defaults for the GenAI SDK. These are plain dict
# writes — no credential discovery, no network, no client construction — so
# importing this module stays side-effect-free in the sense the recipe
# standards require. The SDK reads them lazily when it first makes a call;
# vertexai.init() is deferred to app.config.init_vertex(), invoked by the
# MCP server when it builds its client.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
os.environ["GOOGLE_CLOUD_LOCATION"] = LLM_LOCATION

# MCP connection: SSE if MCP_SERVER_URL is set, otherwise stdio (subprocess)
if MCP_SERVER_URL:
    mcp_connection = SseConnectionParams(
        url=MCP_SERVER_URL, timeout=MCP_CONNECTION_TIMEOUT
    )
else:
    mcp_connection = StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
        ),
        timeout=MCP_CONNECTION_TIMEOUT,
    )

instruction = """\
You are a knowledge base assistant. Your role is to help users find accurate \
information from the indexed documents.

Rules:
- ALWAYS use the ask_knowledge_base tool before answering. Pass a summary of \
the conversation and the user's question. The tool searches the knowledge base \
and returns an answer grounded in the documents.
- Use the tool's response as the basis for your answer. You may rephrase or \
restructure for clarity, but do not add information beyond what was provided.
- Reply in the same language as the user.
- Be concise and direct.
- If a question is ambiguous, ask for clarification rather than guessing."""


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=LLM,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=instruction,
    tools=[McpToolset(connection_params=mcp_connection)],
)

app = App(
    root_agent=root_agent,
    name="app",
)
