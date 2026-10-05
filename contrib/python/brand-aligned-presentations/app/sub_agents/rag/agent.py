# Copyright 2026 Google LLC
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

"""Specialist agent for retrieving internal Consulting Agency knowledge using Vertex AI Search."""

from google.adk.agents import Agent
from google.adk.tools import AgentTool, FunctionTool, VertexAiSearchTool

from ...shared_libraries.config import DATASTORE_ID, ROOT_MODEL
from .prompt import RAG_AGENT_INSTRUCTION


def dummy_search(query: str) -> str:
    """Returns a default message when the internal database is not configured."""
    return "Internal database is not configured or is unavailable."


# 1. Instantiate the RAG tool
if DATASTORE_ID:
    vertex_search_tool = VertexAiSearchTool(data_store_id=DATASTORE_ID)
else:
    vertex_search_tool = FunctionTool(func=dummy_search)

# 2. Define the Internal Researcher Agent
rag_agent = Agent(
    model=ROOT_MODEL,
    name="cymbal_internal_knowledge_expert_agent",
    mode="single_turn",
    description="A specialist agent that retrieves proprietary Consulting Agency assets, frameworks, "
    "and precedents relevant to a client's project using the Vertex AI Search tool.",
    instruction=RAG_AGENT_INSTRUCTION,
    tools=[vertex_search_tool],
)

# Create the final tool that the main agent will use
internal_knowledge_search_tool = AgentTool(agent=rag_agent)
