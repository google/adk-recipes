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

"""Tools for the Underwriting Agent — mock internal data lookup."""

import json

from google.adk.tools.tool_context import ToolContext

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)

# Mock internal business records (simulates the bank's internal database)
# In production, this would query the bank's actual internal system or API
MOCK_INTERNAL_RECORDS = {
    "SBL-2025-00142": {
        "business_name": "Coffee Roasters LLC",
        "business_type": "LLC",
        "ein": "00-1234567",
        "owner_name": "Jane Doe",
        "owner_email": "jane.doe@example.com",
        "owner_phone": "(555) 010-0100",
        "business_address": {
            "street": "742 Evergreen Terrace",
            "city": "Springfield",
            "state": "IL",
            "zip_code": "62704",
        },
        "industry": "Food & Beverage",
        "years_in_business": "6",
        "annual_revenue": "$850,000",
        "net_profit": "$120,000",
        "number_of_employees": "12",
        "existing_debt": "None",
        "existing_loans": [],
        "credit_score": 720,
        "account_standing": "Good",
    },
}


def get_internal_business_data(tool_context: ToolContext) -> dict:
    """
    Retrieve the bank's internal business records for underwriting validation.

    In production, this would query the bank's internal systems.
    For this demo, returns mock data matching the sample loan applications.

    Args:
        tool_context: The tool context with access to session state.

    Returns:
        dict: Internal business data for the given loan request.
    """
    try:
        loan_request_id = tool_context.state.get("loan_request_id")

        if not loan_request_id:
            return {
                "status": "error",
                "message": "loan_request_id not found in session state",
            }

        logger.info(f"Fetching internal records for: {loan_request_id}")

        internal_data = MOCK_INTERNAL_RECORDS.get(loan_request_id)

        if not internal_data:
            # Fall back to default mock record for any loan ID (demo purposes)
            default_record = next(iter(MOCK_INTERNAL_RECORDS.values()))
            internal_data = {**default_record}
            logger.info(
                f"No exact match for {loan_request_id}, using default mock record"
            )

        return {
            "status": "success",
            "loan_request_id": loan_request_id,
            "internal_data": json.dumps(internal_data),
        }

    except Exception as e:
        logger.error(f"Error fetching internal business data: {e}")
        return {"status": "error", "message": f"Error: {e!s}"}
