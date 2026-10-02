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

"""Data models for loan decision."""

from typing import Literal

from pydantic import BaseModel, Field


class LoanDecisionResult(BaseModel):
    """Structured output model for the final loan decision."""

    status: Literal["success", "error"] = Field(
        description="Status of the decision operation"
    )
    decision: Literal["APPROVED", "DENIED", "CONDITIONAL"] = Field(
        description="Final loan decision"
    )
    decision_letter_id: str = Field(
        description="Generated decision letter reference ID (e.g., 'DL-2025-00142-001')"
    )
    approved_amount: str = Field(
        default="",
        description="Approved loan amount (may differ from requested amount)",
    )
    approved_rate: str = Field(
        default="",
        description="Approved interest rate",
    )
    approved_term: str = Field(
        default="",
        description="Approved loan term",
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="Any conditions attached to the approval",
    )
    message: str = Field(description="Summary message about the decision")
