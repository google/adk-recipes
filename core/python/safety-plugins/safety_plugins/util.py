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

"""Utility functions for Guardian."""

from google.adk import runners
from google.genai import types

Runner = runners.Runner


async def run_prompt(
    user_id: str,
    app_name: str,
    runner: Runner,
    message: types.Content,
    session_id: str | None = None,
) -> tuple[str, str]:
    """Runs a prompt using the provided runner and returns the response.

    Args:
        user_id: The ID of the user.
        app_name: The name of the application.
        runner: The runner to use for running the prompt.
        message: The content of the message to send.
        session_id: The ID of an existing session.

    Returns:
        The response text from the agent.
    """
    try:
        if session_id is not None:
            session = await runner.session_service.get_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
        else:
            session = await runner.session_service.create_session(
                user_id=user_id,
                app_name=app_name,
            )
        if not session:
            raise ValueError("Session is None")

        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=message
        ):
            if (
                event.is_final_response()
                and event.content
                and event.content.parts
            ):
                return (
                    event.author,
                    (event.content.parts[0].text or ""),
                )

    except Exception as e:
        return "SYSTEM", str(e)

    return f"{runner.agent.name}", "No response from the agent."
