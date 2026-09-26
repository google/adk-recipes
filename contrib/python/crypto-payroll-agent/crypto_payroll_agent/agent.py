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

"""Root agent for the Crypto Payroll Agent recipe."""

from google.adk.agents import LlmAgent

# Spraay batch tools merged in google/adk-python-community#95.
from google.adk_community.tools.spraay import (  # type: ignore[import]
    spraay_batch_eth,
    spraay_batch_eth_variable,
    spraay_batch_token,
    spraay_batch_token_variable,
)

from .config import CONFIG
from .guardrails import enforce_batch_limits
from .prompt import ROOT_AGENT_INSTRUCTION
from .tools import lookup_token_info, split_pool_proportionally

root_agent = LlmAgent(
    name=CONFIG.agent_name,
    model=CONFIG.model,
    # The spend ceilings and batch limits are enforced here, in Python,
    # before any Spraay tool can sign. The instruction states them too,
    # but only this callback can actually stop a call.
    before_tool_callback=enforce_batch_limits,
    description=(
        "Batch-pays multiple recipients in ETH or ERC-20 tokens on Base "
        "via the Spraay protocol. Supports equal-amount payroll, variable-"
        "amount contractor pay, and proportional pool splits for airdrops "
        "and revenue sharing."
    ),
    instruction=ROOT_AGENT_INSTRUCTION.format(
        max_batch_usd=str(CONFIG.max_batch_usd),
        max_batch_eth=str(CONFIG.max_batch_eth),
    ),
    tools=[
        # Local helpers (must be called before the batch tools when needed).
        lookup_token_info,
        split_pool_proportionally,
        # Spraay batch tools (on-chain action).
        spraay_batch_eth,
        spraay_batch_token,
        spraay_batch_eth_variable,
        spraay_batch_token_variable,
    ],
)
