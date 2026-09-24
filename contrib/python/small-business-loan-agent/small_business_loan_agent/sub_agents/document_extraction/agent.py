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

"""Document Extraction Agent definition."""

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
from small_business_loan_agent.sub_agents.document_extraction.models import (
    LoanApplicationData,
)
from small_business_loan_agent.sub_agents.document_extraction.prompt import (
    DOCUMENT_EXTRACTION_PROMPT,
)
from small_business_loan_agent.sub_agents.document_extraction.tools import (
    inject_document_into_request,
)

MODEL_NAME = os.getenv("MODEL_NAME")

document_extraction_agent = LlmAgent(
    name="DocumentExtractionAgent",
    model=GeminiPreview(model=MODEL_NAME),
    generate_content_config=GenerateContentConfig(
        http_options=HttpOptions(
            retry_options=HttpRetryOptions(initial_delay=1, attempts=2),
        ),
    ),
    instruction=DOCUMENT_EXTRACTION_PROMPT,
    description="Extracts structured loan application data from uploaded documents using Gemini's multimodal capabilities",
    before_agent_callback=[before_agent_callback_with_state_check],
    before_model_callback=inject_document_into_request,
    after_agent_callback=[after_agent_callback_with_state_logging],
    output_schema=LoanApplicationData,
    output_key="DocumentExtractionAgent_output",
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
)
