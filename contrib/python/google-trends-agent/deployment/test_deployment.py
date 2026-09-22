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

import asyncio
import json
import os
import sys

import vertexai
from dotenv import load_dotenv
from google.adk.sessions import VertexAiSessionService
from vertexai import agent_engines

# Add the project root to sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

MAX_ARGS = 100
MAX_RESPONSE_LENGTH = 100


def pretty_print_event(event):
    """Pretty prints an event with truncation for long content."""
    if "content" not in event:
        print(f"[{event.get('author', 'unknown')}]: {event}")
        return

    author = event.get("author", "unknown")
    parts = event["content"].get("parts", [])

    for part in parts:
        if "text" in part:
            text = part["text"]
            print(f"[{author}]: {text}")
        elif "functionCall" in part:
            func_call = part["functionCall"]
            print(
                f"[{author}]: Function call: {func_call.get('name', 'unknown')}"
            )
            # Truncate args if too long
            args = json.dumps(func_call.get("args", {}))
            if len(args) > MAX_ARGS:
                args = args[: MAX_ARGS - 3] + "..."
            print(f"  Args: {args}")
        elif "functionResponse" in part:
            func_response = part["functionResponse"]
            print(
                f"[{author}]: Function response: {func_response.get('name', 'unknown')}"
            )
            # Truncate response if too long
            response = json.dumps(func_response.get("response", {}))
            if len(response) > MAX_RESPONSE_LENGTH:
                response = response[: MAX_RESPONSE_LENGTH - 3] + "..."
            print(f"  Response: {response}")


def main() -> None:
    """Interactively query the deployed Agent Engine from the terminal."""
    # Construct the path to the .env file
    dotenv_path = os.path.join(project_root, ".env")
    load_dotenv(dotenv_path=dotenv_path)

    # The deployed engine lives in a region (see deployment/deploy.py), so use
    # the Agent Engine hosting region here, not the model-serving location.
    agent_engine_location = os.getenv("AGENT_ENGINE_LOCATION", "us-central1")

    vertexai.init(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=agent_engine_location,
    )

    session_service = VertexAiSessionService(
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=agent_engine_location,
    )
    agent_engine_id = os.getenv("AGENT_ENGINE_ID")
    print("AGENT_ENGINE_ID", agent_engine_id)

    session = asyncio.run(
        session_service.create_session(
            app_name=agent_engine_id,
            user_id="123",
        )
    )

    agent_engine = agent_engines.get(agent_engine_id)

    print("Type 'quit' to exit.")
    while True:
        user_input = input("Input: ")
        if user_input == "quit":
            break

        for event in agent_engine.stream_query(
            user_id="123", session_id=session.id, message=user_input
        ):
            pretty_print_event(event)

    asyncio.run(
        session_service.delete_session(
            user_id="123", session_id=session.id, app_name=agent_engine_id
        )
    )


if __name__ == "__main__":
    main()
