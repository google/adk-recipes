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

"""Data models for pricing."""

from typing import Literal

from pydantic import BaseModel, Field


class PricingResult(BaseModel):
    """Structured output model for the pricing calculation."""

    status: Literal["success", "error"] = Field(
        description="Status of the pricing calculation"
    )
    interest_rate: str = Field(
        description="Calculated annual interest rate (e.g., '7.25%')"
    )
    monthly_payment: str = Field(
        description="Estimated monthly payment (e.g., '$2,985')"
    )
    total_interest: str = Field(
        description="Total interest over the loan term (e.g., '$29,100')"
    )
    risk_tier: str = Field(
        description="Risk tier used for pricing (e.g., 'Tier 2 - Moderate Risk')"
    )
    rate_justification: str = Field(
        description="Brief explanation of how the rate was determined"
    )
