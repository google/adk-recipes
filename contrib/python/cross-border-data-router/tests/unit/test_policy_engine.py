# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for the cross-border data-residency routing policy."""

from app.policy.cards import AGENT_REGISTRY, geographic_metadata
from app.policy.engine import evaluate_routing_policy
from app.policy.models import DataRequest


def test_agent_registry_cards_carry_geographic_metadata() -> None:
    """Every card in the registry must expose the geographic extension."""
    assert len(AGENT_REGISTRY) == 3
    for card in AGENT_REGISTRY:
        geo = geographic_metadata(card)
        assert geo["jurisdiction"]
        assert geo["data_residency_regions"]


def test_eu_origin_pii_routes_to_a_eu_eea_compliant_agent() -> None:
    """EU-resident PII must land on an agent whose card covers EU/EEA."""
    request = DataRequest(
        data_classification="PII",
        origin_region="Germany",
        required_residency=("EU",),
    )
    decision = evaluate_routing_policy(request)

    assert decision.decision == "approved"
    assert decision.selected_agent_id in {"eu_processor", "uk_processor"}
    assert decision.score_breakdown, "expected scored candidates on approval"


def test_preferred_jurisdiction_wins_among_compliant_candidates() -> None:
    """Given a tie on residency, the preferred jurisdiction should win."""
    request = DataRequest(
        data_classification="PII",
        origin_region="Germany",
        required_residency=("EU",),
        preferred_jurisdictions=("UK",),
    )
    decision = evaluate_routing_policy(request)

    assert decision.decision == "approved"
    assert decision.selected_agent_id == "uk_processor"


def test_excluded_jurisdiction_is_never_selected_even_if_only_option() -> None:
    """A request cannot be routed to a jurisdiction it explicitly excludes,
    even when that jurisdiction is the only one that satisfies residency."""
    request = DataRequest(
        data_classification="financial",
        origin_region="United States",
        required_residency=("US",),
        excluded_jurisdictions=("US",),
    )
    decision = evaluate_routing_policy(request)

    assert decision.decision == "rejected"
    assert decision.selected_agent_id is None
    assert "us_processor" in decision.eliminated


def test_no_compliant_agent_rejects_rather_than_falling_back() -> None:
    """No card supports APAC residency, so the request must be rejected —
    never silently routed to a non-compliant region."""
    request = DataRequest(
        data_classification="PII",
        origin_region="Singapore",
        required_residency=("APAC",),
    )
    decision = evaluate_routing_policy(request)

    assert decision.decision == "rejected"
    assert decision.selected_agent_id is None
    assert len(decision.eliminated) == 3
    assert not decision.score_breakdown


def test_us_financial_with_no_constraints_routes_to_us_processor() -> None:
    """A plain US-resident request should route to the US processor."""
    request = DataRequest(
        data_classification="financial",
        origin_region="United States",
        required_residency=("US",),
    )
    decision = evaluate_routing_policy(request)

    assert decision.decision == "approved"
    assert decision.selected_agent_id == "us_processor"
    assert decision.jurisdiction == "US"
