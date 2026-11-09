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
Travel Concierge — FinOps-Optimized ADK Multi-Agent App (`travel_optimized`).

Demonstrates all 5 ADK FinOps best practices tracked by `adk-tokenomics-plugin`:
  1. Model Tiering      : `gemini-3.8-flash` for routing & lookups; `gemini-4-argon` only for itinerary synthesis.
  2. Context Caching    : `ContextCacheConfig(min_tokens=1024, ttl_seconds=300)` on `App`.
  3. History Compaction : `EventsCompactionConfig(compaction_interval=4, overlap_size=1)` on `App`.
  4. Modular Skills     : Slim router prompt + on-demand `SkillToolset` (`japan-guide`, `europe-guide`).
  5. Thinking Budgets   : `MINIMAL` on router, `LOW` on lookup specialist, `MEDIUM` on itinerary planner.
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
    FLASH_MODEL,
    FRONTIER_MODEL,
    AgentTool,
    App,
    ContextCacheConfig,
    EventsCompactionConfig,
    FunctionTool,
    LlmAgent,
    build_planner,
    load_travel_skill_toolset,
)
from examples._shared.travel_tools import (
    get_city_weather,
    search_flights,
    search_hotels,
)

MODEL_NAME: str = FLASH_MODEL


def build_travel_optimized(
    auto_export_path: str | None = None,
) -> tuple[App, LlmAgent, TokenControlTower]:
    """Builds the FinOps-optimized 3-agent Travel Concierge ADK App."""
    env_export = os.environ.get("TOKENOMICS_AUTO_EXPORT_PATH")
    export_path = (
        auto_export_path
        or env_export
        or str(
            _REPO_ROOT
            / "examples"
            / "artifacts"
            / "traces"
            / "live_travel_opt.json"
        )
    )

    tower = TokenControlTower(mode="dev", default_model=FLASH_MODEL)
    tokenomics_plugin = tower.create_adk_plugin(
        app_name="travel_optimized",
        auto_export_path=export_path,
    )

    # 1. Fast Lookup Specialist (`gemini-3.8-flash` + LOW thinking)
    finder_planner, finder_cfg = build_planner(FLASH_MODEL, "low")
    flight_hotel_finder = LlmAgent(
        name="flight_hotel_finder",
        model=FLASH_MODEL,
        description="Searches round-trip flights, hotels within budget, and destination weather.",
        instruction=(
            "You are `flight_hotel_finder`. Call `search_flights`, `search_hotels`, and "
            "`get_city_weather` for the requested city and return a concise summary of fares, "
            "nightly hotel rates, and weather."
        ),
        tools=[
            FunctionTool(search_flights),
            FunctionTool(search_hotels),
            FunctionTool(get_city_weather),
        ],
        planner=finder_planner,
        generate_content_config=finder_cfg,
    )

    # 2. Itinerary Synthesis Specialist (`gemini-4-argon` + MEDIUM thinking)
    itin_planner, itin_cfg = build_planner(FRONTIER_MODEL, "medium")
    itinerary_planner = LlmAgent(
        name="itinerary_planner",
        model=FRONTIER_MODEL,
        description="Crafts polished day-by-day travel itineraries and itemized trip budgets.",
        instruction=(
            "You are `itinerary_planner`. Using the flight, hotel, weather, and regional guide "
            "details already gathered in the conversation, create a clear day-by-day itinerary "
            "with a total USD trip cost breakdown (flights + hotel nights + daily local spend). "
            "If the user later asks to search a new city or change flights/hotels, transfer "
            "back to `concierge_router`."
        ),
        planner=itin_planner,
        generate_content_config=itin_cfg,
    )

    # 3. Root Concierge Router (`gemini-3.8-flash` + MINIMAL thinking + on-demand SkillToolset)
    router_planner, router_cfg = build_planner(FLASH_MODEL, "minimal")
    concierge_router = LlmAgent(
        name="concierge_router",
        model=FLASH_MODEL,
        description="Friendly travel concierge that coordinates lookups, guides, and itineraries.",
        instruction=(
            "You are a friendly Travel Concierge (`concierge_router`).\n"
            "1. For simple greetings or quick questions, reply warmly and concisely.\n"
            "2. When planning a trip to Japan or Europe, load the matching travel skill "
            "(`japan-guide` or `europe-guide`) for local transit and neighborhood tips.\n"
            "3. Call `flight_hotel_finder` to gather flight fares, hotels, and weather.\n"
            "4. For full multi-day trip plans, transfer to `itinerary_planner` once options "
            "are gathered so it can synthesize the final itinerary and budget."
        ),
        tools=[
            AgentTool(agent=flight_hotel_finder, include_plugins=True),
            load_travel_skill_toolset(),
        ],
        sub_agents=[itinerary_planner],
        planner=router_planner,
        generate_content_config=router_cfg,
    )

    travel_app = App(
        name="travel_optimized",
        root_agent=concierge_router,
        plugins=[tokenomics_plugin],
        context_cache_config=ContextCacheConfig(
            min_tokens=1024, ttl_seconds=300
        ),
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=4,
            overlap_size=1,
        ),
    )
    tokenomics_plugin.attach_to_agent(concierge_router)
    return travel_app, concierge_router, tower


app, root_agent, tower = build_travel_optimized()
