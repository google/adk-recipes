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

"""Location scoring sub-agent."""

import os

from google.adk.agents import LlmAgent

from ...tools.places import geocode_address, nearby_search, text_search
from .prompt import LOCATION_PROMPT

location_agent = LlmAgent(
    name="location_agent",
    model=os.getenv("MODEL_NAME"),
    description=(
        "Scores a target location 0-100 for business suitability across "
        "competition density, accessibility, and demand signal dimensions. "
        "Input: task description with lat, lng, business_type, and "
        "radius_meters. Output: JSON LocationScore object."
    ),
    instruction=LOCATION_PROMPT,
    tools=[nearby_search, text_search, geocode_address],
)
