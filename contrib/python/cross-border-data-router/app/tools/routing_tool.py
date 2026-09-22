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

"""The tool the root agent calls to make (and only to make) the routing
decision — separately from actually executing the work. Keeping evaluation
and execution as two distinct steps is what makes the decision auditable:
the orchestrator must not hand a record to a sub-agent it hasn't cleared.
"""

from __future__ import annotations

from dataclasses import asdict

from ..policy.engine import evaluate_routing_policy
from ..policy.models import DataRequest


def evaluate_and_route(
    data_classification: str,
    origin_region: str,
    required_residency: list[str],
    excluded_jurisdictions: list[str] | None = None,
    preferred_jurisdictions: list[str] | None = None,
) -> dict:
    """Evaluates cross-border data-residency policy and picks a compliant agent.

    Checks the request against the Agent Card registry's declared geographic
    metadata (jurisdiction, data residency regions, cross-border
    restrictions) and returns a routing decision. Call this before calling
    any regional processor agent — never guess which region to use.

    Args:
        data_classification: What kind of data this is, e.g. "PII",
            "financial", "public".
        origin_region: Where the data/record originates, e.g. "Germany",
            "EU", "United States".
        required_residency: Region codes the data must stay within, e.g.
            ["EU", "EEA"]. Derive this from the data's origin and any
            regulation it names (GDPR implies EU/EEA, CCPA implies US, etc.).
        excluded_jurisdictions: Jurisdictions that must never process this
            record, e.g. ["US"] for an EU customer whose contract forbids
            US transfer. Defaults to none excluded.
        preferred_jurisdictions: Jurisdictions to prefer among otherwise
            eligible agents, e.g. ["UK"] to keep processing close to the
            requester. Defaults to no preference.

    Returns:
        A dict with `decision` ("approved" or "rejected"), and on approval
        `selected_agent_id` — the name of the sub-agent tool to call next —
        plus `selection_reasons` and `score_breakdown` for transparency. On
        rejection, `reason` explains why every candidate was eliminated;
        do not attempt to process the record yourself or pick a fallback —
        relay the rejection to the caller instead.
    """
    request = DataRequest(
        data_classification=data_classification,
        origin_region=origin_region,
        required_residency=tuple(required_residency),
        excluded_jurisdictions=tuple(excluded_jurisdictions or ()),
        preferred_jurisdictions=tuple(preferred_jurisdictions or ()),
    )
    decision = evaluate_routing_policy(request)
    return asdict(decision)
