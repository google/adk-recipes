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

"""Competitor analysis sub-agent."""

import os

from google.adk.agents import LlmAgent

from ...tools.places import nearby_search, place_details, text_search
from .prompt import COMPETITOR_PROMPT

competitor_agent = LlmAgent(
    name="competitor_agent",
    model=os.getenv("MODEL_NAME"),
    description=(
        "Finds and scores all nearby competitors for a given business type "
        "at a target location. Input: task description with lat, lng, "
        "business_type, and radius_meters. "
        "Output: JSON array of competitor objects with competitive scores."
    ),
    instruction=COMPETITOR_PROMPT,
    tools=[nearby_search, place_details, text_search],
)
