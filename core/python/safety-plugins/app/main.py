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

"""Main file for the Guardian agent."""

import asyncio

from absl import app, flags
from google.adk import runners
from google.genai import types

from . import util
from .agent import root_agent
from .plugins import agent_as_a_judge, model_armor

LlmAsAJudge = agent_as_a_judge.LlmAsAJudge
ModelArmorSafetyFilter = model_armor.ModelArmorSafetyFilterPlugin
InMemoryRunner = runners.InMemoryRunner


USER_ID = "user"
APP_NAME = "test_app_with_plugin"

# Define the command-line flag using absl.flags.
FLAGS = flags.FLAGS
flags.DEFINE_enum(
    "plugin",
    "none",
    ["llm_judge", "model_armor", "none"],
    "Specify the safety plugin to enable.",
)


async def main():
    """Runs a multiturn conversation with the agent and the attached plugin."""
    # You can now access the flag's value via FLAGS.plugin.
    plugin_name = FLAGS.plugin

    plugins = []
    if plugin_name == "llm_judge":
        plugins.append(LlmAsAJudge())
        print("Using LlmAsAJudge plugin.")
    elif plugin_name == "model_armor":
        plugins.append(ModelArmorSafetyFilter())
        print("Using ModelArmorSafetyFilter plugin.")
    else:
        print("No plugin activated.")

    # Initialize plugins based on the command-line argument.
    runner = InMemoryRunner(
        agent=root_agent,
        app_name=APP_NAME,
        plugins=plugins,
    )
    session = await runner.session_service.create_session(
        user_id=USER_ID,
        app_name=APP_NAME,
    )

    user_input = input(f"[{USER_ID}]: ")

    while user_input != "exit":
        author, message = await util.run_prompt(
            USER_ID,
            APP_NAME,
            runner,
            types.Content(
                role="user", parts=[types.Part.from_text(text=user_input)]
            ),
            session_id=session.id,
        )
        print(f"[{author}]: {message}")

        user_input = input(f"[{USER_ID}]: ")


if __name__ == "__main__":
    app.run(lambda _: asyncio.run(main()))
