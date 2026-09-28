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

"""Unit tests for pure business logic functions (no GCP dependencies)."""

from small_business_loan_agent.callbacks.before_tool_callback import (
    determine_halt_action,
)
from small_business_loan_agent.sub_agents.pricing.tools import (
    _determine_risk_tier,
    _parse_dollar_amount,
)

# --- _parse_dollar_amount ---


class TestParseDollarAmount:
    def test_standard_format(self):
        assert _parse_dollar_amount("$150,000") == 150000.0

    def test_with_decimals(self):
        assert _parse_dollar_amount("$2,940.97") == 2940.97

    def test_no_symbol(self):
        assert _parse_dollar_amount("850000") == 850000.0

    def test_empty_string(self):
        assert _parse_dollar_amount("") == 0.0

    def test_none(self):
        assert _parse_dollar_amount(None) == 0.0

    def test_no_digits(self):
        assert _parse_dollar_amount("N/A") == 0.0


# --- _determine_risk_tier ---


class TestDetermineRiskTier:
    def test_eligible_no_flags(self):
        tier, rate = _determine_risk_tier(
            {"eligibility_status": "ELIGIBLE", "risk_flags": []}
        )
        assert tier == "Tier 1 - Low Risk"
        assert rate == 6.50

    def test_eligible_with_flags(self):
        tier, rate = _determine_risk_tier(
            {
                "eligibility_status": "ELIGIBLE",
                "risk_flags": ["High debt ratio"],
            }
        )
        assert tier == "Tier 2 - Moderate Risk"
        assert rate == 7.75

    def test_review(self):
        tier, rate = _determine_risk_tier(
            {"eligibility_status": "REVIEW", "risk_flags": []}
        )
        assert tier == "Tier 3 - Elevated Risk"
        assert rate == 9.25

    def test_ineligible(self):
        tier, rate = _determine_risk_tier(
            {"eligibility_status": "INELIGIBLE", "risk_flags": []}
        )
        assert tier == "Tier 4 - High Risk"
        assert rate == 11.00

    def test_missing_status_defaults_to_review_tier(self):
        # A missing eligibility_status falls back to REVIEW, which is the
        # Tier 3 elevated-risk band -- not the Tier 4 high-risk band.
        tier, rate = _determine_risk_tier({})
        assert tier == "Tier 3 - Elevated Risk"
        assert rate == 9.25


# --- determine_halt_action ---


class TestDetermineHaltAction:
    def test_active_status_allows_proceed(self):
        result = determine_halt_action("UnderwritingAgent", "active", [])
        assert result is None

    def test_pending_approval_halts(self):
        issues = [
            {"description": "Missing field: loan_amount", "resolved": False}
        ]
        result = determine_halt_action(
            "UnderwritingAgent", "pending_approval", issues
        )
        assert result is not None
        assert "Cannot proceed" in result["error"]
        assert "Missing field: loan_amount" in result["error"]

    def test_pending_approval_uses_first_unresolved_issue(self):
        issues = [
            {"description": "Resolved issue", "resolved": True},
            {"description": "Active issue", "resolved": False},
        ]
        result = determine_halt_action(
            "PricingAgent", "pending_approval", issues
        )
        assert "Active issue" in result["error"]

    def test_failed_status_halts(self):
        result = determine_halt_action("PricingAgent", "failed", [])
        assert result is not None
        assert "failed" in result["error"].lower()

    def test_completed_status_halts(self):
        result = determine_halt_action(
            "DocumentExtractionAgent", "completed", []
        )
        assert result is not None
        assert "completed" in result["error"].lower()

    def test_none_status_allows_proceed(self):
        result = determine_halt_action("UnderwritingAgent", None, [])
        assert result is None
