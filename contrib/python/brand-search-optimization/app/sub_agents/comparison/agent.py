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

"""Defines title comparison and evaluation agents."""

from google.adk.agents import LlmAgent

from ...shared_libraries import constants
from . import prompt

comparison_generator_agent = LlmAgent(
    model=constants.MODEL,
    name="comparison_generator_agent",
    description="Generates detailed title comparison and search optimization proposals.",
    instruction=prompt.COMPARISON_AGENT_PROMPT,
    output_key="comparison_proposals",
)

comparison_critic_agent = LlmAgent(
    model=constants.MODEL,
    name="comparison_critic_agent",
    description="Critiques title optimization proposals for quality, brand accuracy, and search intent alignment.",
    instruction=prompt.COMPARISON_CRITIC_AGENT_PROMPT,
    output_key="critique_feedback",
)

comparison_root_agent = LlmAgent(
    model=constants.MODEL,
    name="comparison_root_agent",
    description="Coordinates comparison generation and critique to produce the final optimization report.",
    instruction=prompt.COMPARISON_ROOT_AGENT_PROMPT,
    sub_agents=[comparison_generator_agent, comparison_critic_agent],
    output_key="final_optimization_report",
)
