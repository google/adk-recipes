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

# ruff: noqa: E402
"""
Travel Concierge — Unoptimized Baseline ADK App (`travel_unoptimized`).

Intentionally demonstrates the 5 common multi-agent tokenomics anti-patterns so
`adk-tokenomics profile` and `adk-tokenomics assert` flag them automatically:
  1. Frontier model (`gemini-4-argon`) used on every agent, even simple routing & lookups.
  2. No `ContextCacheConfig` on `App` (re-pays full input rate on every turn).
  3. No `EventsCompactionConfig` on `App` (conversation history snowballs across turns).
  4. ~5,500-token static destination manual pasted directly into `concierge_router`.
  5. Unbounded `ThinkingLevel.HIGH` reasoning enabled on all 3 agents.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adk_tokenomics_plugin import TokenControlTower
from examples._shared.adk_compat import (
    FRONTIER_MODEL,
    AgentTool,
    App,
    FunctionTool,
    LlmAgent,
    build_planner,
)
from examples._shared.travel_tools import (
    build_bloated_inline_travel_manual,
    get_city_weather,
    search_flights,
    search_hotels,
)

MODEL_NAME: str = FRONTIER_MODEL


def build_travel_unoptimized(
    auto_export_path: str | None = None,
) -> tuple[App, LlmAgent, TokenControlTower]:
    """Builds the unoptimized baseline 3-agent Travel Concierge ADK App."""
    env_export = os.environ.get("TOKENOMICS_AUTO_EXPORT_PATH")
    export_path = (
        auto_export_path
        or env_export
        or str(
            _REPO_ROOT
            / "examples"
            / "artifacts"
            / "traces"
            / "live_travel_unopt.json"
        )
    )

    tower = TokenControlTower(mode="dev", default_model=FRONTIER_MODEL)
    tokenomics_plugin = tower.create_adk_plugin(
        app_name="travel_unoptimized",
        auto_export_path=export_path,
    )
    # Optional custom SLA callback example ($0.05 session ceiling)
    tower.add_recommendation_rule(
        lambda ctx: (
            f"🚨 [Session Budget Alert]: Spend (${ctx['total_cost']:.4f}) exceeded $0.0500 target."
            if float(ctx.get("total_cost", 0.0)) > 0.05
            else None
        )
    )

    high_planner, high_cfg = build_planner(FRONTIER_MODEL, "high")

    flight_hotel_finder = LlmAgent(
        name="flight_hotel_finder",
        model=FRONTIER_MODEL,
        description="Searches round-trip flights, hotels within budget, and destination weather.",
        instruction=(
            "You are `flight_hotel_finder`. Call `search_flights`, `search_hotels`, and "
            "`get_city_weather` for the requested city and return a summary of options."
        ),
        tools=[
            FunctionTool(search_flights),
            FunctionTool(search_hotels),
            FunctionTool(get_city_weather),
        ],
        planner=high_planner,
        generate_content_config=high_cfg,
    )

    itinerary_planner = LlmAgent(
        name="itinerary_planner",
        model=FRONTIER_MODEL,
        description="Crafts day-by-day travel itineraries and itemized trip budgets.",
        instruction=(
            "You are `itinerary_planner`. Build a day-by-day itinerary and USD budget "
            "breakdown from the flight, hotel, and weather details in the conversation. "
            "If the user later asks to search a new city or change flights/hotels, transfer "
            "back to `concierge_router`."
        ),
        planner=high_planner,
        generate_content_config=high_cfg,
    )

    concierge_router = LlmAgent(
        name="concierge_router",
        model=FRONTIER_MODEL,
        description="Travel concierge router (with bloated inline destination manual).",
        instruction=(
            "You are a friendly Travel Concierge (`concierge_router`).\n"
            "Call `flight_hotel_finder` to gather flight, hotel, and weather options, and "
            "transfer to `itinerary_planner` for full multi-day trip itineraries.\n\n"
            + build_bloated_inline_travel_manual()
        ),
        tools=[AgentTool(agent=flight_hotel_finder, include_plugins=True)],
        sub_agents=[itinerary_planner],
        planner=high_planner,
        generate_content_config=high_cfg,
    )

    travel_app = App(
        name="travel_unoptimized",
        root_agent=concierge_router,
        plugins=[tokenomics_plugin],
        context_cache_config=None,
        events_compaction_config=None,
    )
    tokenomics_plugin.attach_to_agent(concierge_router)
    return travel_app, concierge_router, tower


app, root_agent, tower = build_travel_unoptimized()
