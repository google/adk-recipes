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

"""Data shapes for the cross-border routing policy engine.

These mirror the request/response shapes described in the OpenEAGO
specification's Phase 2 (Planning & Negotiation) discovery flow
(https://openeago.finos.org/), simplified down to the two dimensions this
recipe teaches: data residency and jurisdiction exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DataRequest:
    """A request to process a data record, as the caller declares it.

    Attributes:
        data_classification: What kind of data this is, e.g. "PII",
            "financial", "public". Informational — carried through to the
            decision for audit purposes.
        origin_region: Where the data subject/record originates, e.g.
            "Germany", "EU". Informational — carried through for audit.
        required_residency: Region codes the data must stay within. An
            agent is only eligible if its card's
            ``data_residency_regions`` covers every one of these.
        excluded_jurisdictions: Jurisdictions the data must never be
            processed in, regardless of residency claims (e.g. a
            contractual cross-border restriction).
        preferred_jurisdictions: Jurisdictions to prefer among otherwise
            eligible agents. Soft — used only for scoring, never for
            elimination.
    """

    data_classification: str
    origin_region: str
    required_residency: tuple[str, ...]
    excluded_jurisdictions: tuple[str, ...] = ()
    preferred_jurisdictions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate agent that survived the hard filters, with its score."""

    agent_id: str
    jurisdiction: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of evaluating a `DataRequest` against the agent registry.

    Attributes:
        decision: "approved" or "rejected".
        selected_agent_id: The `AgentCard.name` to route to, or None when
            rejected.
        jurisdiction: The selected agent's declared jurisdiction, or None
            when rejected.
        reason: A one-line human-readable explanation of the outcome.
        selection_reasons: Why the winner beat the other survivors (empty
            on rejection).
        score_breakdown: Per-candidate scores considered, most relevant
            first (empty on rejection unless candidates survived stage 1/2
            but none could be scored, which should not happen).
        eliminated: Agent ids removed by the hard filters, with why.
    """

    decision: str
    selected_agent_id: str | None
    jurisdiction: str | None
    reason: str
    selection_reasons: list[str] = field(default_factory=list)
    score_breakdown: list[ScoredCandidate] = field(default_factory=list)
    eliminated: dict[str, str] = field(default_factory=dict)
