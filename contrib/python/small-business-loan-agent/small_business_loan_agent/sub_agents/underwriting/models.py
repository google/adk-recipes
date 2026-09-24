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

"""Data models for underwriting — combines validation and eligibility checks."""

from typing import Literal

from pydantic import BaseModel, Field


class FieldComparison(BaseModel):
    """Comparison of a field between the application and internal records."""

    field_name: str = Field(description="Name of the field being compared")
    application_value: str = Field(
        description="Value from the loan application document"
    )
    internal_value: str = Field(
        description="Value from the bank's internal records"
    )
    match_status: Literal["Match", "No Match", "Partial Match"] = Field(
        description="Whether the values match"
    )
    notes: str = Field(
        default="", description="Additional notes about the comparison"
    )


class UnderwritingReport(BaseModel):
    """Structured output model for the underwriting assessment."""

    # Validation results
    validation_status: Literal["MATCH", "NO MATCH"] = Field(
        description="Overall validation status — whether application data matches internal records"
    )
    matched_fields: list[FieldComparison] = Field(
        default_factory=list,
        description="Fields that match between the application and internal records",
    )
    discrepancies: list[FieldComparison] = Field(
        default_factory=list,
        description="Fields that don't match between the application and internal records",
    )

    # Eligibility results
    eligibility_status: Literal["ELIGIBLE", "INELIGIBLE", "REVIEW"] = Field(
        description="Whether the business meets eligibility criteria: ELIGIBLE, INELIGIBLE, or REVIEW (needs manual review)"
    )
    matched_rule: str | None = Field(
        default=None,
        description="The eligibility rule that determined the status (e.g., 'Revenue exceeds minimum threshold')",
    )
    risk_flags: list[str] = Field(
        default_factory=list,
        description="List of risk flags identified during underwriting",
    )

    summary: str = Field(
        description="Executive summary of the underwriting assessment"
    )
    recommendation: str = Field(
        description="Recommendation on whether to proceed with the loan"
    )
