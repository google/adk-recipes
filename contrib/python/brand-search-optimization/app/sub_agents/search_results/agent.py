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

from google.adk.agents import LlmAgent

from ...shared_libraries import constants
from ...tools.browser_computer import get_computer_use_toolset
from . import prompt

search_results_agent = LlmAgent(
    model=constants.MODEL,
    name="search_results_agent",
    description="Visually navigates retail search engines using Computer Use to observe top competitor product titles.",
    instruction=prompt.SEARCH_RESULT_AGENT_PROMPT,
    tools=[
        get_computer_use_toolset(),
    ],
    output_key="observed_search_results",
)
