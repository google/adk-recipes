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

"""US regional data processor sub-agent.

Its `name` ("us_processor") matches the `AgentCard.name` in
`app/policy/cards.py` — that's the string the routing policy returns as
`selected_agent_id`, and what the root orchestrator uses to pick which of
its `AgentTool`s to invoke.
"""

import os

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types


def process_record(record_summary: str) -> str:
    """Simulates running a record through this region's processing pipeline.

    Args:
        record_summary: A short description of the data/record to process.

    Returns:
        A confirmation string describing where and under what controls the
        record was processed.
    """
    return (
        "Processed in the US region (Iowa data center) under CCPA "
        f"compliant controls. Record: {record_summary}"
    )


def create_us_processor_agent() -> Agent:
    """Creates a fresh, isolated instance of the US processor agent."""
    return Agent(
        name="us_processor",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "Processes customer data within the United States under CCPA "
            "controls. Only eligible once the routing policy has approved "
            "US as the compliant jurisdiction for this record."
        ),
        instruction=(
            "You are the US-based data processor. Call `process_record` "
            "with a short description of the record you were handed, then "
            "relay its confirmation message back verbatim."
        ),
        tools=[process_record],
    )


us_processor_agent = create_us_processor_agent()
