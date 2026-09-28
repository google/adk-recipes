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

import datetime
import os
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext  # Memory Bank
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.preload_memory_tool import (
    PreloadMemoryTool,
)  # Memory Bank
from google.genai import types


def get_weather(query: str) -> str:
    """Simulates a web search. Use it get information on weather.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        A string with the simulated weather information for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


# --- Memory Bank ---
# This callback triggers memory generation after each agent turn. On Agent
# Engine Runtime, it sends session events to VertexAiMemoryBankService, which
# extracts user preferences and facts for retrieval in future sessions.
#
# Alternative: use callback_context.add_events_to_memory(events=...) to send
# only a subset of events, which is better for incremental processing.
# See: https://docs.cloud.google.com/agent-builder/agent-engine/memory-bank/quickstart-adk
async def generate_memories_callback(callback_context: CallbackContext):
    """Sends the session's events to Memory Bank for memory generation."""
    await callback_context.add_session_to_memory()


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        # Defaulted to .env.example's value: the image ships no .env, so a
        # container deployed without MODEL_NAME otherwise fails at import.
        model=os.getenv("MODEL_NAME", "gemini-3.5-flash"),
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are a helpful AI assistant designed to provide accurate and useful "
        "information. You remember user preferences and facts from previous "
        "conversations. Use your memory to personalize responses."
    ),
    tools=[
        get_weather,
        get_current_time,
        # --- Memory Bank ---
        # PreloadMemoryTool retrieves memories at the start of each turn and
        # injects them into the system instruction. The model sees past user
        # preferences/facts as context without needing an explicit tool call.
        #
        # Alternative: LoadMemoryTool() — the model calls it on-demand when it
        # decides memories are needed, giving more control but less consistency.
        PreloadMemoryTool(),
    ],
    # --- Memory Bank ---
    after_agent_callback=generate_memories_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
