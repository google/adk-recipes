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

"""Data models for document extraction — small business loan application."""

from typing import Literal

from pydantic import BaseModel, Field


class BusinessAddress(BaseModel):
    """Model for business address."""

    street: str = Field(
        default="", description="Street address of the business"
    )
    city: str = Field(default="", description="City")
    state: str = Field(default="", description="State code (e.g., IL, CA)")
    zip_code: str = Field(default="", description="ZIP code")


class LoanApplicationData(BaseModel):
    """Structured output model for small business loan application data."""

    # Business information
    business_name: str = Field(
        default="",
        description="Legal name of the business applying for the loan",
    )
    business_type: str = Field(
        default="",
        description="Legal structure of the business (e.g., 'LLC', 'S-Corp', 'Sole Proprietorship', 'Partnership')",
    )
    ein: str = Field(
        default="",
        description="Employer Identification Number (EIN) of the business",
    )
    industry: str = Field(
        default="",
        description="Industry or business sector (e.g., 'Food & Beverage', 'Automotive Services')",
    )
    years_in_business: str = Field(
        default="",
        description="Number of years the business has been operating",
    )
    number_of_employees: str = Field(
        default="",
        description="Number of employees in the business",
    )
    business_address: BusinessAddress | None = Field(
        default=None,
        description="Physical address of the business",
    )

    # Owner information
    owner_name: str = Field(
        default="",
        description="Full name of the business owner or primary applicant",
    )
    owner_email: str = Field(
        default="",
        description="Email address of the business owner",
    )
    owner_phone: str = Field(
        default="",
        description="Phone number of the business owner",
    )

    # Financial information
    annual_revenue: str = Field(
        default="",
        description="Annual revenue of the business (e.g., '$850,000')",
    )
    net_profit: str = Field(
        default="",
        description="Annual net profit of the business (e.g., '$120,000')",
    )
    existing_debt: str = Field(
        default="",
        description="Total existing business debt (e.g., '$50,000' or 'None')",
    )

    # Loan details
    loan_amount_requested: str = Field(
        default="",
        description="Amount of loan requested (e.g., '$150,000')",
    )
    loan_purpose: Literal[
        "Equipment",
        "Expansion",
        "Working Capital",
        "Real Estate",
        "Refinance",
        "Other",
        "",
    ] = Field(
        default="",
        description="Purpose of the loan",
    )
    loan_term_months: str = Field(
        default="",
        description="Requested loan term in months (e.g., '60')",
    )
    collateral_offered: str = Field(
        default="",
        description="Description of collateral offered to secure the loan",
    )
