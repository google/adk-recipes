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

"""Defines the root Brand Search Optimization Agent."""

from google.adk.agents import LlmAgent
from google.adk.apps import App

from . import prompt
from .shared_libraries import constants
from .sub_agents import (
    comparison_root_agent,
    keyword_finding_agent,
    search_results_agent,
)

root_agent = LlmAgent(
    model=constants.MODEL,
    name=constants.AGENT_NAME,
    description=constants.DESCRIPTION,
    instruction=prompt.ROOT_PROMPT,
    sub_agents=[
        keyword_finding_agent,
        search_results_agent,
        comparison_root_agent,
    ],
)

app = App(root_agent=root_agent, name="brand_search_optimization")
