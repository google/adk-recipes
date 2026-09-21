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

"""Defines visual Search Results Agent powered by Gemini Computer Use."""

import copy
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.models.llm_request import LlmRequest

from ...shared_libraries import constants
from ...tools.browser_computer import get_computer_use_toolset
from . import prompt

_TOOL_NAME_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    (
        "click_at",
        (
            "click",
            "double_click",
            "triple_click",
            "right_click",
            "middle_click",
        ),
    ),
    ("type_text_at", ("type",)),
    ("hover_at", ("move", "mouse_down", "mouse_up")),
    ("scroll_at", ("scroll",)),
    ("current_state", ("take_screenshot",)),
    ("key_combination", ("press_key", "hotkey", "key_down", "key_up")),
]


async def adapt_computer_use_tools_callback(
    callback_context: Any,
    llm_request: LlmRequest,
) -> None:
    """Registers modern Gemini 3.x action names as renamed copies of legacy tools."""
    for method_name, aliases in _TOOL_NAME_ALIASES:
        tool = llm_request.tools_dict.get(method_name)
        if tool is None:
            continue
        for alias in aliases:
            if alias not in llm_request.tools_dict:
                aliased = copy.copy(tool)
                aliased.name = alias
                llm_request.tools_dict[alias] = aliased


search_results_agent = LlmAgent(
    model=constants.MODEL,
    name="search_results_agent",
    description="Visually navigates retail search engines using Computer Use to observe top competitor product titles.",
    instruction=prompt.SEARCH_RESULT_AGENT_PROMPT,
    tools=[
        get_computer_use_toolset(),
    ],
    before_model_callback=adapt_computer_use_tools_callback,
    output_key="observed_search_results",
)
