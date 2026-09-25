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

import logging

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.planners import BuiltInPlanner
from google.genai import types

from .config import config
from .prompt import (
    get_task_planner_prompt,
    get_technical_designer_prompt,
    get_user_story_refiner_prompt,
)
from .tools.artifact_tools import save_artifact
from .tools.spanner_query_tools import SpannerQueryTools

logger = logging.getLogger(__name__)

tools_enabled = config.is_spanner_configured

if not config.model_name:
    raise ValueError("MODEL_NAME environment variable is required but not set.")

if tools_enabled:
    logger.info("Initializing SDLC agents with Spanner tools enabled.")
    spanner_tools = SpannerQueryTools.get_toolset()
else:
    logger.info("Initializing SDLC agents without Spanner tools.")
    spanner_tools = []

user_story_refiner_agent = LlmAgent(
    name="user_story_refiner",
    model=config.model_name,
    description=(
        "Analyzes requirements or draft stories and refines them into"
        " comprehensive, standardized agile user story work items."
    ),
    instruction=get_user_story_refiner_prompt(tools_enabled=tools_enabled),
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            include_thoughts=True,
        )
    ),
    tools=spanner_tools,
)

technical_designer_agent = LlmAgent(
    name="technical_designer",
    model=config.model_name,
    description=(
        "Analyzes refined user stories and generates concrete, structured RFC"
        " technical designs with Mermaid diagrams and ADRs."
    ),
    instruction=get_technical_designer_prompt(tools_enabled=tools_enabled),
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            include_thoughts=True,
        )
    ),
    tools=spanner_tools,
)

task_planner_agent = LlmAgent(
    name="task_planner",
    model=config.model_name,
    description=(
        "Translates technical design documents and user stories into a"
        " granular, dependency-linked task execution plan."
    ),
    instruction=get_task_planner_prompt(),
    tools=[save_artifact],
)

root_agent = SequentialAgent(
    name="sdlc_workflow_suite",
    description=(
        "End-to-end SDLC workflow suite that sequentially refines user stories,"
        " drafts RFC technical architecture designs, and generates structured"
        " development task execution plans."
    ),
    sub_agents=[
        user_story_refiner_agent,
        technical_designer_agent,
        task_planner_agent,
    ],
)
