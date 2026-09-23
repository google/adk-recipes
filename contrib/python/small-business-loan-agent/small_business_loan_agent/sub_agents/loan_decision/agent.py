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

"""Loan Decision Agent definition."""

import os

from google.adk.agents import LlmAgent
from google.genai.types import (
    GenerateContentConfig,
    HttpOptions,
    HttpRetryOptions,
)

from small_business_loan_agent.gemini_custom import GeminiPreview
from small_business_loan_agent.shared_libraries.firestore_utils.state_callbacks import (
    after_agent_callback_with_state_logging,
    before_agent_callback_with_state_check,
)
from small_business_loan_agent.sub_agents.loan_decision.models import (
    LoanDecisionResult,
)
from small_business_loan_agent.sub_agents.loan_decision.prompt import (
    LOAN_DECISION_PROMPT,
)
from small_business_loan_agent.sub_agents.loan_decision.tools import (
    finalize_loan_decision,
)

MODEL_NAME = os.getenv("MODEL_NAME")

loan_decision_agent = LlmAgent(
    name="LoanDecisionAgent",
    model=GeminiPreview(model=MODEL_NAME),
    generate_content_config=GenerateContentConfig(
        http_options=HttpOptions(
            retry_options=HttpRetryOptions(initial_delay=1, attempts=2),
        ),
    ),
    instruction=LOAN_DECISION_PROMPT,
    description="Finalizes the loan decision and generates a decision letter after human approval",
    before_agent_callback=before_agent_callback_with_state_check,
    tools=[finalize_loan_decision],
    after_agent_callback=after_agent_callback_with_state_logging,
    output_schema=LoanDecisionResult,
    output_key="LoanDecisionAgent_output",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
