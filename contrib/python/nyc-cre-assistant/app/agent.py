# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Root agent for the NYC commercial real estate assistant."""

from __future__ import annotations

import os
import pathlib

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from .tools import (
    find_debt_by_bbl,
    find_owner_by_bbl,
    get_bbl_from_normalized_address,
)

SKILLS_ROOT = pathlib.Path(__file__).parent / "skills"

skills = [
    load_skill_from_dir(SKILLS_ROOT / name)
    for name in [
        "bbl-address",
        "find-owner",
        "find-debt",
    ]
]

skill_toolset = SkillToolset(skills=skills)


def create_agent() -> Agent:
    """Create a fresh agent instance."""
    return Agent(
        name="nyc_cre_assistant",
        model=Gemini(
            model=os.getenv("MODEL_NAME", "gemini-3.5-flash"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "An NYC commercial real estate assistant for address, BBL, "
            "owner, and recorded debt evidence lookups."
        ),
        instruction=(
            "You are an NYC commercial real estate public-record assistant.\n\n"
            "First infer the user's intent and select exactly one route:\n"
            "- Route address or BBL lookup requests to the bbl-address skill.\n"
            "- Route property-owner lookup requests to the find-owner skill.\n"
            "- Route mortgage, lender, debt, or recorded financing lookup "
            "requests to the find-debt skill.\n\n"
            "After selecting a route, load and follow the selected skill's "
            "instructions. Use only tools relevant to the selected route. "
            "Do not use unrelated tools, do not continue researching after "
            "the selected route result, and do not hand off the task "
            "elsewhere.\n\n"
            "Evidence boundaries matter. Summarize public-record evidence "
            "clearly, preserve uncertainty, and do not infer hidden owners, "
            "current loan balance, payoff status, or actual maturity dates "
            "unless a tool result directly proves those facts."
        ),
        tools=[
            get_bbl_from_normalized_address,
            find_owner_by_bbl,
            find_debt_by_bbl,
            skill_toolset,
        ],
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
