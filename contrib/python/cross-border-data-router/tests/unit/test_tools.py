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
"""Unit tests for the `evaluate_and_route` tool and the regional processor
sub-agents' own tools."""

from app.sub_agents.eu_processor.agent import (
    process_record as eu_process_record,
)
from app.sub_agents.uk_processor.agent import (
    process_record as uk_process_record,
)
from app.sub_agents.us_processor.agent import (
    process_record as us_process_record,
)
from app.tools.routing_tool import evaluate_and_route


def test_evaluate_and_route_approves_eu_pii() -> None:
    """The tool should return a plain, JSON-serializable approval dict."""
    result = evaluate_and_route(
        data_classification="PII",
        origin_region="Germany",
        required_residency=["EU"],
    )

    assert result["decision"] == "approved"
    assert result["selected_agent_id"] in {"eu_processor", "uk_processor"}
    assert (
        isinstance(result["selection_reasons"], list)
        and result["selection_reasons"]
    )


def test_evaluate_and_route_rejects_when_excluded_is_only_option() -> None:
    """Excluding the only compliant jurisdiction must produce a rejection,
    not a silent fallback to a different, non-compliant region."""
    result = evaluate_and_route(
        data_classification="PII",
        origin_region="United States",
        required_residency=["US"],
        excluded_jurisdictions=["US"],
    )

    assert result["decision"] == "rejected"
    assert result["selected_agent_id"] is None
    assert result["reason"]


def test_eu_process_record_confirms_region() -> None:
    """The EU processor's own tool should confirm EU handling."""
    result = eu_process_record("customer_123 support ticket")
    assert "EU-WEST" in result
    assert "customer_123 support ticket" in result


def test_us_process_record_confirms_region() -> None:
    """The US processor's own tool should confirm US handling."""
    result = us_process_record("customer_456 support ticket")
    assert "US" in result
    assert "customer_456 support ticket" in result


def test_uk_process_record_confirms_region() -> None:
    """The UK processor's own tool should confirm UK handling."""
    result = uk_process_record("customer_789 support ticket")
    assert "UK" in result
    assert "customer_789 support ticket" in result
