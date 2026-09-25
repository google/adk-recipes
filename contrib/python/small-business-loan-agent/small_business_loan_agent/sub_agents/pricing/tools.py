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

"""Tools for the Pricing Agent — mock loan pricing calculation."""

import re

from google.adk.tools.tool_context import ToolContext

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_LOAN_TERM_MONTHS = 60


def _parse_dollar_amount(value: str) -> float:
    """Parse a dollar string like '$150,000' into a float."""
    if not value:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", value)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _determine_risk_tier(underwriting_output: dict) -> tuple[str, float]:
    """
    Determine risk tier and base rate from underwriting results.

    Returns:
        Tuple of (risk_tier_name, base_interest_rate)
    """
    eligibility = underwriting_output.get("eligibility_status", "REVIEW")
    risk_flags = underwriting_output.get("risk_flags", [])

    if eligibility == "ELIGIBLE" and len(risk_flags) == 0:
        return "Tier 1 - Low Risk", 6.50
    elif eligibility == "ELIGIBLE":
        return "Tier 2 - Moderate Risk", 7.75
    elif eligibility == "REVIEW":
        return "Tier 3 - Elevated Risk", 9.25
    else:
        return "Tier 4 - High Risk", 11.00


def calculate_loan_pricing(tool_context: ToolContext) -> dict:
    """
    Calculate loan pricing based on application data and underwriting results.

    In production, this would query the bank's pricing engine or rate sheet system.
    For this demo, calculates mock pricing based on risk tier.

    Args:
        tool_context: The tool context with access to session state.

    Returns:
        dict: Pricing calculation results.
    """
    try:
        application_data = tool_context.state.get(
            "DocumentExtractionAgent_output"
        )
        underwriting_output = tool_context.state.get("UnderwritingAgent_output")
        loan_request_id = tool_context.state.get("loan_request_id")

        if not application_data:
            return {
                "status": "error",
                "message": "Application data not found. DocumentExtractionAgent must complete first.",
            }

        if not underwriting_output:
            return {
                "status": "error",
                "message": "Underwriting data not found. UnderwritingAgent must complete first.",
            }

        logger.info(f"Calculating pricing for: {loan_request_id}")

        # Parse loan details
        loan_amount = _parse_dollar_amount(
            application_data.get("loan_amount_requested", "0")
        )
        term_months_str = application_data.get(
            "loan_term_months", str(DEFAULT_LOAN_TERM_MONTHS)
        )
        try:
            term_months = (
                int(re.sub(r"[^\d]", "", term_months_str))
                if term_months_str
                else DEFAULT_LOAN_TERM_MONTHS
            )
        except ValueError:
            term_months = DEFAULT_LOAN_TERM_MONTHS

        if loan_amount <= 0:
            return {"status": "error", "message": "Invalid loan amount"}

        # Determine risk tier and rate
        risk_tier, annual_rate = _determine_risk_tier(
            underwriting_output if isinstance(underwriting_output, dict) else {}
        )

        # Calculate monthly payment (standard amortization formula)
        monthly_rate = annual_rate / 100 / 12
        if monthly_rate > 0:
            monthly_payment = (
                loan_amount
                * (monthly_rate * (1 + monthly_rate) ** term_months)
                / ((1 + monthly_rate) ** term_months - 1)
            )
        else:
            monthly_payment = loan_amount / term_months

        total_interest = (monthly_payment * term_months) - loan_amount

        return {
            "status": "success",
            "interest_rate": f"{annual_rate}%",
            "monthly_payment": f"${monthly_payment:,.2f}",
            "total_interest": f"${total_interest:,.2f}",
            "risk_tier": risk_tier,
            "rate_justification": (
                f"Rate of {annual_rate}% based on {risk_tier}. "
                f"Loan amount ${loan_amount:,.0f} over {term_months} months."
            ),
        }

    except Exception as e:
        logger.error(f"Error calculating loan pricing: {e}")
        return {"status": "error", "message": f"Error: {e!s}"}
