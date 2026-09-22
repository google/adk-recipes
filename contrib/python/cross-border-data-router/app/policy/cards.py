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

"""A static registry of regional partner Agent Cards.

The Agent2Agent (A2A) protocol's `AgentCard` (`a2a.types.AgentCard`, the same
type this repo already serves in `core/python/long-horizon-harness`) has no
built-in concept of jurisdiction or data residency — discovery only covers
capabilities. OpenEAGO (https://openeago.finos.org/) proposes filling that
gap with a "compliance layer on top of A2A", carried as a capability
*extension* rather than a protocol change. This module follows that pattern:
every card below declares one `AgentExtension` at `GEOGRAPHIC_EXTENSION_URI`
whose `params` hold the geographic/regulatory metadata OpenEAGO's own docs
use — `jurisdiction`, `data_center`, `geographic_location`,
`data_residency_regions`, `cross_border_restrictions`, and `compliance`.

Each card here describes a regional processing agent this recipe also runs
locally (see `app/sub_agents/`). In a real deployment these cards would
instead be fetched from a live A2A agent registry; this recipe keeps the
registry static and in-process (`architecture.datasources: hardcoded` in
`manifest.yaml`) so the routing policy is the thing under the microscope,
not network/registry plumbing.
"""

from __future__ import annotations

from typing import Any

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentSkill,
)

# Not resolvable — this recipe doesn't serve a live extension spec. It only
# needs to be a stable, unique key so `geographic_metadata()` can find the
# right extension on a card that may carry others.
GEOGRAPHIC_EXTENSION_URI = (
    "https://openeago.finos.org/extensions/geographic-metadata/v1"
)


def _geographic_extension(
    *,
    jurisdiction: str,
    data_center: str,
    geographic_location: str,
    data_residency_regions: list[str],
    cross_border_restrictions: list[str],
    compliance: list[str],
) -> AgentExtension:
    """Builds the OpenEAGO-style geographic-metadata extension for a card."""
    return AgentExtension(
        uri=GEOGRAPHIC_EXTENSION_URI,
        description="OpenEAGO-style jurisdiction and data-residency metadata.",
        required=False,
        params={
            "jurisdiction": jurisdiction,
            "data_center": data_center,
            "geographic_location": geographic_location,
            "data_residency_regions": data_residency_regions,
            "cross_border_restrictions": cross_border_restrictions,
            "compliance": compliance,
        },
    )


def build_regional_agent_card(
    *,
    agent_id: str,
    description: str,
    jurisdiction: str,
    data_center: str,
    geographic_location: str,
    data_residency_regions: list[str],
    cross_border_restrictions: list[str],
    compliance: list[str],
) -> AgentCard:
    """Builds a real `a2a.types.AgentCard` for a regional processing agent.

    `agent_id` doubles as the card's `name` and as the local sub-agent name
    the root orchestrator delegates to once this card is selected (see
    `app/sub_agents/`) — in a networked deployment it would instead be
    resolved to the card's `url`.
    """
    return AgentCard(
        name=agent_id,
        description=description,
        url=f"local://cross-border-data-router/agents/{agent_id}",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(
            extensions=[
                _geographic_extension(
                    jurisdiction=jurisdiction,
                    data_center=data_center,
                    geographic_location=geographic_location,
                    data_residency_regions=data_residency_regions,
                    cross_border_restrictions=cross_border_restrictions,
                    compliance=compliance,
                )
            ],
        ),
        skills=[
            AgentSkill(
                id=f"{agent_id}.process_record",
                name="Process data record",
                description=description,
                tags=["data-processing", jurisdiction.lower()],
            )
        ],
    )


def geographic_metadata(card: AgentCard) -> dict[str, Any]:
    """Reads the geographic/residency metadata back off an Agent Card.

    This is the function the policy engine calls to answer "where does this
    agent say it is, and what is it allowed to touch?" — i.e. it decides
    routing eligibility purely from what the card declares, never from a
    side channel.

    Raises:
        ValueError: if the card carries no geographic-metadata extension.
    """
    extensions = (
        (card.capabilities.extensions or []) if card.capabilities else []
    )
    for extension in extensions:
        if extension.uri == GEOGRAPHIC_EXTENSION_URI:
            return dict(extension.params or {})
    raise ValueError(
        f"Agent card '{card.name}' declares no geographic-metadata "
        f"extension ({GEOGRAPHIC_EXTENSION_URI}); it cannot be routing-"
        "policy evaluated."
    )


AGENT_REGISTRY: list[AgentCard] = [
    build_regional_agent_card(
        agent_id="eu_processor",
        description=(
            "Processes customer data within the EU/EEA under GDPR controls."
        ),
        jurisdiction="EU",
        data_center="GCP-EUW1-FRANKFURT",
        geographic_location="EU-FRANKFURT",
        data_residency_regions=["EU", "EEA"],
        cross_border_restrictions=["US", "CHINA", "RUSSIA"],
        compliance=["Reg:GDPR", "Residency:EU", "Control:EncryptionAtRest"],
    ),
    build_regional_agent_card(
        agent_id="uk_processor",
        description=(
            "Processes customer data within the UK under UK-GDPR controls."
        ),
        jurisdiction="UK",
        data_center="GCP-EUW2-LONDON",
        geographic_location="UK-LONDON",
        data_residency_regions=["UK", "EU"],
        cross_border_restrictions=["US", "CHINA", "RUSSIA"],
        compliance=["Reg:UK-GDPR", "Residency:UK", "Control:EncryptionAtRest"],
    ),
    build_regional_agent_card(
        agent_id="us_processor",
        description=(
            "Processes customer data within the United States under CCPA "
            "controls."
        ),
        jurisdiction="US",
        data_center="GCP-US-CENTRAL1-IOWA",
        geographic_location="US-IOWA",
        data_residency_regions=["US"],
        cross_border_restrictions=[],
        compliance=["Reg:CCPA", "Residency:US", "Control:EncryptionAtRest"],
    ),
]
