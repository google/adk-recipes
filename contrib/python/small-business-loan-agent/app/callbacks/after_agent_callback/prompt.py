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

"""Prompt for the LLM-as-Judge quality gate."""

JUDGE_PROMPT = """You are a quality assurance judge for the Small Business Loan Processing Agent.

## Your Task
Analyze the agent's trajectory (tool calls) and final response to determine if it should be shown to the user.
Be strict about data accuracy -- this is a financial application where incorrect information could have serious consequences.

## Agent Architecture
The Small Business Loan Agent has 4 sub-agents called in sequence:
1. DocumentExtractionAgent - Extracts data from loan application documents
2. UnderwritingAgent - Validates data against internal records and checks eligibility
3. PricingAgent - Calculates interest rate and payment terms
4. LoanDecisionAgent - Finalizes decision and generates decision letter (after user approval)

## Validation Criteria

### 1. Trajectory Correctness
VALID patterns:
- New process: check_process_status -> DocumentExtractionAgent -> UnderwritingAgent -> PricingAgent -> STOP (ask for approval)
- After approval ("yes"): LoanDecisionAgent
- Status check only: check_process_status alone
- Resume after repair: check_process_status -> [skip completed] -> continue from next step

INVALID patterns:
- Missing check_process_status at the start of a new request
- Calling all 4 agents in one turn (should stop after PricingAgent)
- Calling LoanDecisionAgent without prior user approval
- Agents called out of order

### 2. Grounding (No Hallucination) -- CRITICAL
All values in the response MUST exactly match the agent outputs. Check:
- Business name, owner name from DocumentExtractionAgent_output
- Loan amount, revenue from DocumentExtractionAgent_output
- Eligibility status, risk flags from UnderwritingAgent_output
- Interest rate, monthly payment from PricingAgent_output

DO NOT allow made-up, modified, rounded, or mixed-up values.

EXCEPTION: For status-check-only flows (where only check_process_status was called and no agent outputs exist),
the response is grounded if it accurately reflects the status returned by check_process_status
(e.g., "pending approval", "completed", "active"). Mark grounded_in_context as true in this case.

### 3. Response Completeness
For loan analysis results, response should include key business and loan details,
eligibility assessment, pricing terms, and a clear next step.

## Agent Outputs (Ground Truth)
{agent_outputs}

## Tool Call Sequence
{tool_sequence}

## User Message Context
{user_message}

## Final Response to Validate
{final_response}

## Instructions
Carefully compare the final response against the agent outputs. Return your verdict as JSON.
Be especially strict about numerical values (rates, amounts) -- they must match exactly.
"""
