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

"""Programmatic ADK runner used by the GitHub webhook."""

from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import root_agent

APP_NAME = "github_gemini_enterprise_app"
USER_ID = "github-app"


async def run_agent(prompt: str, *, context: str) -> str:
    """Run the root agent once and return the generated text."""
    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(f"GitHub context:\n{context}\n\nUser request:\n{prompt}")
            )
        ],
    )

    response_text = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=message,
    ):
        if not event.content or not event.content.parts:
            continue
        for part in event.content.parts:
            if part.text:
                response_text += part.text

    if not response_text.strip():
        raise RuntimeError("Gemini Enterprise agent returned an empty reply.")
    return response_text.strip()
