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

"""Tools for the Loan Decision Agent — mock decision finalization."""

from google.adk.tools.tool_context import ToolContext

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)

# Maps the UnderwritingAgent's eligibility_status onto a final decision.
# REVIEW (or a missing/unknown status) is referred to a human rather than
# being auto-approved.
_ELIGIBILITY_TO_DECISION = {
    "ELIGIBLE": "APPROVED",
    "INELIGIBLE": "DENIED",
    "REVIEW": "CONDITIONAL",
}
_DEFAULT_DECISION = "CONDITIONAL"


def _resolve_decision(underwriting_data: object) -> tuple[str, str]:
    """Map the underwriting eligibility onto a final decision.

    Returns:
        Tuple of (decision, eligibility_status).
    """
    eligibility = "UNKNOWN"
    if isinstance(underwriting_data, dict):
        eligibility = str(
            underwriting_data.get("eligibility_status") or "UNKNOWN"
        ).upper()
    return (
        _ELIGIBILITY_TO_DECISION.get(eligibility, _DEFAULT_DECISION),
        eligibility,
    )


def finalize_loan_decision(tool_context: ToolContext) -> dict:
    """
    Finalize the loan decision and generate a decision letter reference.

    The decision is derived from the UnderwritingAgent's `eligibility_status`:
    ELIGIBLE -> APPROVED, INELIGIBLE -> DENIED, and REVIEW (or anything
    unrecognised) -> CONDITIONAL, i.e. referred for manual review.

    In production, this would record the decision in the bank's loan origination
    system and trigger generation of official decision letters.

    Args:
        tool_context: The tool context with access to session state.

    Returns:
        dict: Final decision details.
    """
    try:
        loan_request_id = tool_context.state.get("loan_request_id")
        application_data = tool_context.state.get(
            "DocumentExtractionAgent_output"
        )
        underwriting_data = tool_context.state.get("UnderwritingAgent_output")
        pricing_data = tool_context.state.get("PricingAgent_output")

        if not loan_request_id:
            return {
                "status": "error",
                "message": "loan_request_id not found in session state",
            }

        if not application_data:
            return {
                "status": "error",
                "message": "Application data not found in session state",
            }

        if not underwriting_data:
            return {
                "status": "error",
                "message": "Underwriting data not found in session state",
            }

        if not pricing_data:
            return {
                "status": "error",
                "message": "Pricing data not found in session state",
            }

        logger.info(f"Finalizing loan decision for: {loan_request_id}")

        decision, eligibility_status = _resolve_decision(underwriting_data)

        # Generate decision letter ID
        decision_letter_id = f"DL-{loan_request_id.replace('SBL-', '')}-001"

        # Extract approved terms from pricing
        approved_rate = (
            pricing_data.get("interest_rate", "N/A")
            if isinstance(pricing_data, dict)
            else "N/A"
        )
        loan_amount = (
            application_data.get("loan_amount_requested", "N/A")
            if isinstance(application_data, dict)
            else "N/A"
        )
        loan_term = (
            application_data.get("loan_term_months")
            if isinstance(application_data, dict)
            else None
        )
        # Single source of truth for the term string, so the prose message and
        # the structured field can never disagree (avoids "for N/A months").
        # A truthiness check, not `== "N/A"`: the key can be present but null or
        # empty, which an equality guard lets through as "None months".
        approved_term = f"{loan_term} months" if loan_term else "N/A"

        if decision == "APPROVED":
            conditions = [
                "Business insurance verification required within 30 days",
                "Collateral documentation to be submitted before disbursement",
            ]
            message = (
                f"Loan {loan_request_id} has been approved. "
                f"Decision letter {decision_letter_id} has been generated. "
                f"Approved for {loan_amount} at {approved_rate} "
                f"for {approved_term}."
            )
        elif decision == "DENIED":
            conditions = []
            message = (
                f"Loan {loan_request_id} has been denied "
                f"(underwriting eligibility: {eligibility_status}). "
                f"Decision letter {decision_letter_id} has been generated."
            )
        else:
            conditions = ["Manual credit review required before disbursement"]
            message = (
                f"Loan {loan_request_id} has been referred for manual review "
                f"(underwriting eligibility: {eligibility_status}). "
                f"Decision letter {decision_letter_id} has been generated."
            )

        return {
            "status": "success",
            "decision": decision,
            "decision_letter_id": decision_letter_id,
            "approved_amount": loan_amount if decision == "APPROVED" else "N/A",
            "approved_rate": approved_rate if decision == "APPROVED" else "N/A",
            "approved_term": approved_term if decision == "APPROVED" else "N/A",
            "conditions": conditions,
            "message": message,
        }

    except Exception as e:
        logger.error(f"Error finalizing loan decision: {e}")
        return {"status": "error", "message": f"Error: {e!s}"}
