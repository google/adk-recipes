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

"""Specialist agent for performing web research with programmatic grounding extraction."""

from typing import Any

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool, google_search
from google.genai import types

from ...shared_libraries.config import ROOT_MODEL
from .prompt import GOOGLE_RESEARCH_INSTRUCTION

# 1. Define the core specialist agent
research_agent = Agent(
    model=ROOT_MODEL,
    name="research_specialist",
    mode="single_turn",
    description="Gather high-impact facts and statistics using Google Search.",
    instruction=GOOGLE_RESEARCH_INSTRUCTION,
    tools=[google_search],
)


def _extract_grounding_metadata(event: Any) -> list[dict[str, str]]:
    """Helper to traverse the final event and extract web source URIs."""
    sources = []
    seen_urls = set()

    # Robustly find grounding metadata in the event structure
    data = event.model_dump() if hasattr(event, "model_dump") else {}

    def find_metadata(obj):
        if isinstance(obj, dict):
            if "groundingMetadata" in obj:
                return obj["groundingMetadata"]
            # Recurse through all values
            for v in obj.values():
                res = find_metadata(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for v in obj:
                res = find_metadata(v)
                if res:
                    return res
        return None

    metadata = find_metadata(data)
    if metadata:
        for chunk in metadata.get("groundingChunks", []):
            web = chunk.get("web", {})
            url = web.get("uri")
            title = web.get("title", "Source")
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append({"title": title, "url": url})
    return sources


async def google_research_grounded_tool(query: str) -> str:
    """
    Executes research and appends verified URLs extracted from grounding metadata.
    """
    # Setup local execution context for the sub-agent
    # Using InMemorySessionService for research sub-tasks is efficient
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="google_research",
        user_id="research_task",
        session_id="research_session",
    )

    runner = Runner(
        app_name="google_research",
        agent=research_agent,
        session_service=session_service,
    )

    # Execute the research agent
    user_message = types.Content(role="user", parts=[types.Part(text=query)])
    final_event = None
    async for event in runner.run_async(
        user_id="research_task",
        session_id="research_session",
        new_message=user_message,
    ):
        final_event = event

    if not final_event:
        return "Error: Research agent failed to respond."

    # Extract the narrative answer text
    answer_text = ""
    content = getattr(final_event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            if getattr(part, "text", None):
                answer_text += part.text

    # Programmatically extract the verified sources from the underlying grounding chunks
    sources = _extract_grounding_metadata(final_event)

    # Append the verified links to the end of the report to ensure the main agent receives them
    if sources:
        answer_text += (
            "\n\n### Verified Source URLs (Programmatic Grounding):\n"
        )
        for i, src in enumerate(sources, 1):
            answer_text += f"{i}. {src['url']}\n"

    return answer_text


# 2. Export as the final FunctionTool for the main Orchestrator to use
google_research_tool = FunctionTool(func=google_research_grounded_tool)
