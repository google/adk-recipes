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

"""Traffic estimation sub-agent."""

import os

from google.adk.agents import LlmAgent

from ...tools.places import nearby_search, place_details
from .prompt import TRAFFIC_PROMPT

traffic_agent = LlmAgent(
    name="traffic_agent",
    model=os.getenv("MODEL_NAME"),
    description=(
        "Estimates foot traffic and demand at a target location by analyzing "
        "competitor opening hours and review data. "
        "Input: task description with lat, lng, business_type, and "
        "radius_meters. Output: JSON TrafficEstimate object."
    ),
    instruction=TRAFFIC_PROMPT,
    tools=[nearby_search, place_details],
)
