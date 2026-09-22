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

"""Cross-border data-residency routing policy.

Implements a simplified version of the three-stage agent-selection algorithm
OpenEAGO's docs describe for Phase 2 (Planning & Negotiation) discovery:

  Stage 1 — data-residency hard filter: eliminate any candidate whose
    declared `data_residency_regions` does not cover every region the
    request requires.
  Stage 2 — jurisdiction-exclusion hard filter: eliminate any surviving
    candidate whose `jurisdiction` is on the request's excluded list.
  Stage 3 — scoring: rank survivors by jurisdiction preference and
    compliance-tag overlap; ties broken by registry order.

Hard filters run before scoring and never get overridden by it — a
non-compliant agent cannot out-score its way into eligibility. If nothing
survives, the request is rejected outright rather than silently falling
back to a non-compliant agent. This mirrors OpenEAGO's framing of an
unresolvable cross-border conflict as a compliance violation to be escalated
(its `ComplianceViolationError` / human-in-the-loop gate), not a best-effort
routing choice.
"""

from __future__ import annotations

from a2a.types import AgentCard

from .cards import geographic_metadata
from .models import DataRequest, RoutingDecision, ScoredCandidate

# Weights for Stage 3 scoring. Jurisdiction preference dominates; compliance
# overlap is a secondary tiebreaker. (OpenEAGO's own model adds cost/SLA/
# latency terms this recipe deliberately leaves out to keep the residency
# story front and center.)
_JURISDICTION_WEIGHT = 0.7
_COMPLIANCE_WEIGHT = 0.3


def _passes_residency_filter(geo: dict, request: DataRequest) -> bool:
    """Stage 1: every required region must be in the card's residency set."""
    if not request.required_residency:
        return True
    residency = set(geo.get("data_residency_regions", []))
    return set(request.required_residency).issubset(residency)


def _passes_jurisdiction_filter(geo: dict, request: DataRequest) -> bool:
    """Stage 2: the card's own jurisdiction must not be excluded."""
    return geo.get("jurisdiction") not in request.excluded_jurisdictions


def _score(agent_id: str, geo: dict, request: DataRequest) -> ScoredCandidate:
    """Stage 3: weighted jurisdiction-preference + compliance-overlap score."""
    reasons: list[str] = []
    jurisdiction = geo.get("jurisdiction", "")

    if jurisdiction in request.preferred_jurisdictions:
        jurisdiction_score = 1.0
        reasons.append(f"Jurisdiction {jurisdiction} is on the preferred list.")
    elif request.preferred_jurisdictions:
        jurisdiction_score = 0.5
        reasons.append(
            f"Jurisdiction {jurisdiction} is compliant but not preferred."
        )
    else:
        jurisdiction_score = 0.7
        reasons.append(
            f"Jurisdiction {jurisdiction} satisfies the requirement."
        )

    residency = geo.get("data_residency_regions", [])
    reasons.append(
        f"Data residency {residency} covers the required "
        f"{list(request.required_residency)}."
    )

    compliance_tags = set(geo.get("compliance", []))
    relevant_tags = {
        tag
        for tag in compliance_tags
        if request.data_classification.lower() in tag.lower()
        or "reg:" in tag.lower()
        or "residency:" in tag.lower()
    }
    compliance_score = min(len(relevant_tags) / 2, 1.0)
    if relevant_tags:
        reasons.append(f"Compliance tags on record: {sorted(relevant_tags)}.")

    score = (
        _JURISDICTION_WEIGHT * jurisdiction_score
        + _COMPLIANCE_WEIGHT * compliance_score
    )
    return ScoredCandidate(
        agent_id=agent_id,
        jurisdiction=jurisdiction,
        score=round(score, 3),
        reasons=reasons,
    )


def evaluate_routing_policy(
    request: DataRequest,
    candidates: list[AgentCard] | None = None,
) -> RoutingDecision:
    """Evaluates a `DataRequest` against the agent registry and routes.

    Args:
        request: The data-residency request to satisfy.
        candidates: Agent cards to consider. Defaults to the module-level
            `AGENT_REGISTRY`; overridable for tests.

    Returns:
        A `RoutingDecision`: "approved" with the winning agent and its
        rivals' scores, or "rejected" with why every candidate was
        eliminated.
    """
    if candidates is None:
        from .cards import AGENT_REGISTRY

        candidates = AGENT_REGISTRY

    eliminated: dict[str, str] = {}
    survivors: list[tuple[AgentCard, dict]] = []

    for card in candidates:
        geo = geographic_metadata(card)

        if not _passes_residency_filter(geo, request):
            eliminated[card.name] = (
                f"data_residency_regions {geo.get('data_residency_regions')} "
                f"does not cover required {list(request.required_residency)}"
            )
            continue

        if not _passes_jurisdiction_filter(geo, request):
            eliminated[card.name] = (
                f"jurisdiction {geo.get('jurisdiction')!r} is excluded by "
                f"the request ({list(request.excluded_jurisdictions)})"
            )
            continue

        survivors.append((card, geo))

    if not survivors:
        return RoutingDecision(
            decision="rejected",
            selected_agent_id=None,
            jurisdiction=None,
            reason=(
                "No agent card satisfies both the required data residency "
                f"{list(request.required_residency)} and the excluded "
                f"jurisdictions {list(request.excluded_jurisdictions)}. "
                "Escalate for manual (human-in-the-loop) review rather than "
                "falling back to a non-compliant agent."
            ),
            eliminated=eliminated,
        )

    scored = [_score(card.name, geo, request) for card, geo in survivors]
    scored.sort(key=lambda c: c.score, reverse=True)
    winner = scored[0]

    return RoutingDecision(
        decision="approved",
        selected_agent_id=winner.agent_id,
        jurisdiction=winner.jurisdiction,
        reason=f"{winner.agent_id} scored highest among compliant agents.",
        selection_reasons=winner.reasons,
        score_breakdown=scored,
        eliminated=eliminated,
    )
