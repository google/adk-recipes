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

"""Constants shared across the recipe."""

# Maps each sub-agent's name to the session-state key holding its output.
# Declared once here because both the resume logic in `tools/tools.py` and the
# Firestore state callbacks need it, and the two drifting apart would silently
# break repair & resume.
AGENT_OUTPUT_KEY_MAP = {
    "DocumentExtractionAgent": "DocumentExtractionAgent_output",
    "UnderwritingAgent": "UnderwritingAgent_output",
    "PricingAgent": "PricingAgent_output",
    "LoanDecisionAgent": "LoanDecisionAgent_output",
}
