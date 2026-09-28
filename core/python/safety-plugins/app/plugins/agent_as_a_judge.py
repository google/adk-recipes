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

"""Guardian plugin to run steward agents."""

import enum
import logging
import os
from collections.abc import Callable
from typing import Any

from google.adk import runners
from google.adk.agents import invocation_context, llm_agent
from google.adk.events import event
from google.adk.models import llm_response
from google.adk.plugins import base_plugin
from google.adk.tools import base_tool, tool_context
from google.genai import types

from .. import prompts, util

Event = event.Event
InMemoryRunner = runners.InMemoryRunner
CallbackContext = base_plugin.CallbackContext
ToolContext = tool_context.ToolContext
InvocationContext = invocation_context.InvocationContext
LlmAgent = llm_agent.LlmAgent
BasePlugin = base_plugin.BasePlugin
LlmResponse = llm_response.LlmResponse
BaseTool = base_tool.BaseTool

_USER_PROMPT_REMOVED_MESSAGE = (
    "A safety filter has removed the last user prompt as it was deemed unsafe."
)
_UNSAFE_TOOL_INPUT_MESSAGE = "Unable to call tool due to unsafe inputs."
_UNSAFE_TOOL_OUTPUT_MESSAGE = (
    "Unable to emit tool result due to unsafe tool output."
)
_MODEL_RESPONSE_REMOVED_MESSAGE = (
    "A safety filter has removed the model's response as it was deemed unsafe."
)

default_jailbreak_safety_agent = LlmAgent(
    model=os.getenv("MODEL_NAME_GENERATED_2"),
    name="jailbreak_safety_agent",
    instruction=prompts.JAILBREAK_FILTER_INSTRUCTION,
)

default_safety_analysis_parser = lambda analysis: "UNSAFE" in analysis  # noqa: E731


class JudgeOn(enum.StrEnum):
    """Enum for the different callbacks to run the judge on."""

    USER_MESSAGE = "user_message"
    BEFORE_TOOL_CALL = "before_tool_call"
    TOOL_OUTPUT = "tool_output"
    MODEL_OUTPUT = "model_output"


class LlmAsAJudge(BasePlugin):
    """A custom plugin that runs an LLM as a judge."""

    def __init__(
        self,
        judge_agent: LlmAgent = default_jailbreak_safety_agent,
        analysis_parser: Callable[[str], bool] = default_safety_analysis_parser,
        judge_on: set[str] | None = None,
    ) -> None:
        """Initialize the plugin.

        Args:
          judge_agent: The agent to use as the judge.
          judge_on: A list of callbacks to run the judge on. Can contain
            'user_message', 'before_tool_call', 'tool_output', 'model_output'.
            Defaults to ['user_message', 'tool_output'].
          analysis_parser: A function to parse the judge's analysis and return a
            boolean indicating if the message is unsafe or not. True indicates
            unsafe, False indicates safe.
        """
        super().__init__(name="judge_agent")

        self._judge_agent = judge_agent
        self._judge_user_id = self._judge_agent.name
        self._judge_app_name = "judge_app"

        self._runner = InMemoryRunner(
            agent=self._judge_agent,
            app_name=self._judge_app_name,
        )

        if judge_on is None:
            judge_on = {JudgeOn.USER_MESSAGE, JudgeOn.TOOL_OUTPUT}

        self._judge_on = judge_on
        self._analysis_parser = analysis_parser

    async def _is_unsafe(self, message: str) -> bool:
        """Runs the LLM as a judge on the given message."""

        author, judge_analysis = await util.run_prompt(
            user_id=self._judge_user_id,
            app_name=self._judge_app_name,
            runner=self._runner,
            message=types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=message),
                ],
            ),
        )
        is_unsafe = self._analysis_parser(judge_analysis)
        logging.debug(
            "[%s]: `%s` (is_unsafe: %s)", author, judge_analysis, is_unsafe
        )
        return is_unsafe

    async def on_user_message_callback(
        self,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        if JudgeOn.USER_MESSAGE not in self._judge_on:
            return None
        message = (
            f"<user_message>\n{user_message.parts[0].text}\n</user_message>"
        )
        if await self._is_unsafe(message):
            # Set the state to false if the user prompt is unsafe and return a
            # modified user prompt. This will be consumed by the before_run_callback
            # to halt the runner and end the invocation before the user prompt is
            # sent to the model.
            invocation_context.session.state["is_user_prompt_safe"] = False
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text=_USER_PROMPT_REMOVED_MESSAGE)],
            )

    async def before_run_callback(
        self,
        invocation_context: InvocationContext,
    ) -> types.Content | None:
        # Consume the state set in the `on_user_message_callback` to determine if the
        # user prompt is safe. If not, return a modified user prompt.
        if not invocation_context.session.state.get(
            "is_user_prompt_safe", True
        ):
            # Reset session state to true to allow the runner to proceed normally.
            invocation_context.session.state["is_user_prompt_safe"] = True
            return types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=_USER_PROMPT_REMOVED_MESSAGE,
                    )
                ],
            )

    async def before_tool_callback(
        self,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        if JudgeOn.BEFORE_TOOL_CALL not in self._judge_on:
            return None
        message = (
            f"<tool_call>\nTool call: {tool.name}({tool_args!s})\n</tool_call>"
        )
        if await self._is_unsafe(message):
            return {"error": _UNSAFE_TOOL_INPUT_MESSAGE}

    async def after_tool_callback(
        self,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if JudgeOn.TOOL_OUTPUT not in self._judge_on:
            return None
        message = f"<tool_output>\n{result!s}\n</tool_output>"
        if await self._is_unsafe(message):
            return {"error": _UNSAFE_TOOL_OUTPUT_MESSAGE}

    async def after_model_callback(
        self,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        if JudgeOn.MODEL_OUTPUT not in self._judge_on:
            return None
        llm_content = llm_response.content
        if not llm_content or not llm_content.parts:
            return None
        # Support for multiple parts and different types of LLM responses
        # (e.g. function calls etc.).
        model_output = "\n".join(
            [part.text or "" for part in llm_content.parts]
        ).strip()
        if not model_output:
            return None
        message = f"<model_output>\n{model_output}\n</model_output>"
        if await self._is_unsafe(message):
            return types.Content(
                role="model",
                parts=[
                    types.Part.from_text(text=_MODEL_RESPONSE_REMOVED_MESSAGE)
                ],
            )
