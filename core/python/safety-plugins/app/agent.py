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

"""Agent definitions for the Safety Plugins recipe."""

import os

from google.adk.agents import LlmAgent

from . import prompts, tools

AGENT_MODEL = os.getenv("MODEL_NAME_GENERATED_1")

sub_agent = LlmAgent(
    model=AGENT_MODEL,
    instruction=prompts.SUB_AGENT_SI,
    name="sub_agent",
    tools=[tools.fib_tool, tools.io_bound_tool],
)

root_agent = LlmAgent(
    model=AGENT_MODEL,
    instruction=prompts.ROOT_AGENT_SI,
    name="main_agent",
    tools=[tools.short_sum_tool, tools.long_sum_tool],
    sub_agents=[sub_agent],
)
