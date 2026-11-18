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

"""ADK Voice Agent: Gemini Live voice orchestrator for coding threads on the user's computer.

This module only wires the agent together; the voice behavior lives in `app.voice`.
"""

from __future__ import annotations

from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.app_utils.model_locations import live_model_location
from app.app_utils.models import live_model
from app.tools.look_tools import look
from app.tools.memory_tools import episode_tools
from app.tools.navigation_tools import navigate
from app.tools.thread_tools import (
    approve,
    cancel_thread,
    list_projects,
    list_threads,
    message_thread,
    name_project,
    publish_thread,
    read_thread,
    share_context,
    start_thread,
)
from app.voice.callbacks import hydrate_threads
from app.voice.instruction import build_orchestrator_instruction
from app.voice.non_blocking import non_blocking_tool, thread_tool
from app.voice.preferences import get_current_time_and_timezone
from app.voice.voice_mode import VoiceAgent, VoiceSessionPlugin

# The native-audio Live model, called in LIVE_MODEL_LOCATION (app/app_utils/model_locations.py).
MODEL = live_model()

# Native by default; a user's cascaded sessions open a CascadeLive instead (app/voice/voice_mode.py).
root_agent = VoiceAgent(
    name="voice_agent",
    model=Gemini(
        model=MODEL,
        # Live models are regional; every other model uses GOOGLE_CLOUD_LOCATION.
        client_kwargs={"location": live_model_location()},
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=build_orchestrator_instruction,
    before_agent_callback=hydrate_threads,
    tools=[
        thread_tool(start_thread),
        read_thread,
        non_blocking_tool(look),
        thread_tool(message_thread),
        thread_tool(approve),
        thread_tool(share_context),
        list_threads,
        non_blocking_tool(list_projects),
        non_blocking_tool(name_project),
        non_blocking_tool(cancel_thread),
        non_blocking_tool(publish_thread),
        navigate,
        get_current_time_and_timezone,
        *episode_tools(),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    plugins=[VoiceSessionPlugin()],
)

__all__ = ["MODEL", "app", "root_agent"]
