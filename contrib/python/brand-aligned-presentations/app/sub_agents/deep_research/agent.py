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

"""Specialist agent for performing deep research with programmatic grounding extraction."""

import re

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from ...shared_libraries.config import ROOT_MODEL
from .prompt import DEEP_RESEARCH_INSTRUCTION
from .tools.deep_research_tool import deep_research_tool

# 1. Define the core specialist agent
research_agent = Agent(
    model=ROOT_MODEL,
    name="deep_research_specialist",
    mode="single_turn",
    description="Perform deep web research to gather comprehensive information, statistics, and facts.",
    instruction=DEEP_RESEARCH_INSTRUCTION,
    tools=[deep_research_tool],
)


def _extract_urls_from_text(text: str) -> list[str]:
    """Helper to extract raw URLs from the agent's text response."""
    url_pattern = r"https?://[^\s\]\)\>]+"
    found_urls = re.findall(url_pattern, text)
    unique_urls = []
    for url in found_urls:
        # Clean trailing punctuation
        clean_url = re.sub(r"[\.\,\)\}\]]$", "", url)
        if clean_url not in unique_urls:
            unique_urls.append(clean_url)
    return unique_urls


async def deep_research_grounded_tool(query: str) -> str:
    """
    Executes deep research and ensures raw URLs are preserved and appended.
    """
    # Setup local execution context for the sub-agent
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="deep_research",
        user_id="deep_research_task",
        session_id="deep_research_session",
    )

    runner = Runner(
        app_name="deep_research",
        agent=research_agent,
        session_service=session_service,
    )

    # Execute the research agent
    user_message = types.Content(role="user", parts=[types.Part(text=query)])
    final_event = None
    async for event in runner.run_async(
        user_id="deep_research_task",
        session_id="deep_research_session",
        new_message=user_message,
    ):
        final_event = event

    if not final_event:
        return "Error: Deep Research agent failed to respond."

    # Extract the narrative answer text
    answer_text = ""
    content = getattr(final_event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            if getattr(part, "text", None):
                answer_text += part.text

    # Programmatically extract URLs from the text to ensure they are listed explicitly
    # This acts as a safety net to ensure data provenance for the Slide Writer
    sources = _extract_urls_from_text(answer_text)

    if sources:
        # If the text already contains a "References" or "Sources" section from the tool,
        # we check to avoid excessive duplication, but prioritize explicit listing.
        if "### Extracted Sources:" not in answer_text:
            answer_text += "\n\n### Verified Source URLs (Deep Research):\n"
            for i, url in enumerate(sources, 1):
                answer_text += f"{i}. {url}\n"

    return answer_text


# 2. Export as the final FunctionTool for the main Orchestrator
deep_research_agent_tool = FunctionTool(func=deep_research_grounded_tool)
