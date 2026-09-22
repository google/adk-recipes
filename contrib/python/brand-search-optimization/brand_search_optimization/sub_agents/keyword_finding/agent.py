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

"""Defines keyword finding agent for brand catalog analysis."""

from google.adk.agents import LlmAgent

from ...shared_libraries import constants
from ...tools.bq_connector import get_product_details_for_brand
from . import prompt

keyword_finding_agent = LlmAgent(
    model=constants.MODEL,
    name="keyword_finding_agent",
    description="Extracts and ranks high-intent shopper search keywords from brand catalog data.",
    instruction=prompt.KEYWORD_FINDING_AGENT_PROMPT,
    tools=[
        get_product_details_for_brand,
    ],
    output_key="extracted_keywords",
)
