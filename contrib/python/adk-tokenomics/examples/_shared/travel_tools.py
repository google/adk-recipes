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

"""
Deterministic Travel Concierge Tools (`search_flights`, `search_hotels`, `get_city_weather`).

Zero external API keys required — works offline in tests and live in `adk web`
for any destination city a user asks about.
"""

from __future__ import annotations

from typing import Any

from adk_tokenomics_plugin import TokenControlTower, track_tokens

_FLIGHTS_CATALOG: dict[str, list[dict[str, Any]]] = {
    "tokyo": [
        {
            "airline": "ANA",
            "flight": "NH107",
            "route": "Nonstop",
            "roundtrip_usd": 840,
            "duration_hrs": 11.0,
        },
        {
            "airline": "ZIPAIR",
            "flight": "ZG25",
            "route": "Nonstop Value",
            "roundtrip_usd": 590,
            "duration_hrs": 11.2,
        },
    ],
    "kyoto": [
        {
            "airline": "JAL (via KIX Osaka)",
            "flight": "JL69",
            "route": "1-stop + Haruka Express",
            "roundtrip_usd": 820,
            "duration_hrs": 12.5,
        },
    ],
    "paris": [
        {
            "airline": "Air France",
            "flight": "AF83",
            "route": "Nonstop CDG",
            "roundtrip_usd": 760,
            "duration_hrs": 10.5,
        },
        {
            "airline": "French bee",
            "flight": "BF711",
            "route": "Nonstop Value ORY",
            "roundtrip_usd": 520,
            "duration_hrs": 10.8,
        },
    ],
    "london": [
        {
            "airline": "British Airways",
            "flight": "BA286",
            "route": "Nonstop LHR",
            "roundtrip_usd": 710,
            "duration_hrs": 10.2,
        },
    ],
    "rome": [
        {
            "airline": "ITA Airways",
            "flight": "AZ641",
            "route": "Nonstop FCO",
            "roundtrip_usd": 790,
            "duration_hrs": 12.0,
        },
    ],
    "new york": [
        {
            "airline": "JetBlue",
            "flight": "B6-416",
            "route": "Nonstop JFK",
            "roundtrip_usd": 310,
            "duration_hrs": 5.5,
        },
    ],
}

_HOTELS_CATALOG: dict[str, list[dict[str, Any]]] = {
    "tokyo": [
        {
            "name": "Hotel Groove Shinjuku",
            "neighborhood": "Shinjuku",
            "nightly_usd": 185,
            "rating": 4.6,
        },
        {
            "name": "NOHGA Hotel Ueno Tokyo",
            "neighborhood": "Ueno / Asakusa",
            "nightly_usd": 145,
            "rating": 4.7,
        },
        {
            "name": "Shibuya Stream Hotel",
            "neighborhood": "Shibuya",
            "nightly_usd": 240,
            "rating": 4.8,
        },
    ],
    "kyoto": [
        {
            "name": "The Thousand Kyoto",
            "neighborhood": "Kyoto Station",
            "nightly_usd": 210,
            "rating": 4.8,
        },
        {
            "name": "Piece Hostel Sanjo",
            "neighborhood": "Downtown Nakagyo",
            "nightly_usd": 95,
            "rating": 4.6,
        },
    ],
    "paris": [
        {
            "name": "Hotel Jeanne d'Arc Le Marais",
            "neighborhood": "Le Marais (4th)",
            "nightly_usd": 195,
            "rating": 4.6,
        },
        {
            "name": "CitizenM Paris Gare de Lyon",
            "neighborhood": "12th Arr.",
            "nightly_usd": 160,
            "rating": 4.5,
        },
    ],
    "london": [
        {
            "name": "The Hoxton Southwark",
            "neighborhood": "South Bank",
            "nightly_usd": 215,
            "rating": 4.7,
        },
    ],
}

_WEATHER_CATALOG: dict[str, dict[str, Any]] = {
    "tokyo": {
        "condition": "Crisp & Clear",
        "temp_c": "14-20°C (57-68°F)",
        "tip": "Pack comfortable walking shoes and load a digital Suica card on your phone.",
    },
    "kyoto": {
        "condition": "Mild & Sunny",
        "temp_c": "13-19°C (55-66°F)",
        "tip": "Visit Fushimi Inari and Arashiyama before 8:00 AM to beat crowds.",
    },
    "paris": {
        "condition": "Partly Cloudy",
        "temp_c": "11-17°C (52-63°F)",
        "tip": "Book Louvre and Orsay timed-entry tickets online in advance.",
    },
    "london": {
        "condition": "Light Breeze",
        "temp_c": "10-16°C (50-61°F)",
        "tip": "Use contactless tap-to-pay directly on Tube and Elizabeth Line gates.",
    },
}


def search_flights(
    destination: str,
    origin: str = "SFO",
    trip_days: int = 3,
) -> dict[str, Any]:
    """Searches round-trip flight options and fares for a destination city."""
    city_clean = str(destination or "Tokyo").strip()
    key = city_clean.lower()
    matches = next(
        (v for k, v in _FLIGHTS_CATALOG.items() if k in key),
        [
            {
                "airline": "Star Alliance Express",
                "flight": "SA204",
                "route": f"Nonstop from {origin}",
                "roundtrip_usd": 620,
                "duration_hrs": 8.5,
            },
            {
                "airline": "SkyValue Saver",
                "flight": "SV118",
                "route": f"1-stop from {origin}",
                "roundtrip_usd": 440,
                "duration_hrs": 10.5,
            },
        ],
    )
    return {
        "origin": origin,
        "destination": city_clean,
        "trip_days": max(1, int(trip_days)),
        "options": matches,
    }


def search_hotels(
    city: str,
    max_nightly_usd: int = 300,
) -> dict[str, Any]:
    """Searches top-rated hotels in a city within a nightly USD budget."""
    city_clean = str(city or "Tokyo").strip()
    key = city_clean.lower()
    cap = max(50, int(max_nightly_usd))
    catalog: list[dict[str, Any]] = next(
        (v for k, v in _HOTELS_CATALOG.items() if k in key),
        [
            {
                "name": f"{city_clean} Central Boutique Hotel",
                "neighborhood": "City Center",
                "nightly_usd": min(175, cap),
                "rating": 4.6,
            },
            {
                "name": f"{city_clean} Harbor Inn",
                "neighborhood": "Waterfront",
                "nightly_usd": min(130, cap),
                "rating": 4.4,
            },
        ],
    )
    filtered = [h for h in catalog if int(h["nightly_usd"]) <= cap] or catalog
    return {
        "city": city_clean,
        "max_nightly_usd": cap,
        "hotels": filtered,
    }


def get_city_weather(city: str) -> dict[str, Any]:
    """Returns typical weather conditions and a local packing/transit tip."""
    city_clean = str(city or "Tokyo").strip()
    key = city_clean.lower()
    info = next(
        (v for k, v in _WEATHER_CATALOG.items() if k in key),
        {
            "condition": "Pleasant & Mild",
            "temp_c": "15-22°C (59-72°F)",
            "tip": "Pack layers and comfortable walking shoes for neighborhood exploring.",
        },
    )
    return {"city": city_clean, **info}


def build_bloated_inline_travel_manual() -> str:
    """Builds a ~5,500-token monolithic travel manual to demonstrate the unoptimized prompt anti-pattern."""
    base_block = (
        "GLOBAL DESTINATION REFERENCE HANDBOOK — STATIC INLINE ENCYCLOPEDIA:\n"
        "- Tokyo Transit: Use IC cards (Suica/Pasmo) on Yamanote, Ginza, and Marunouchi lines; "
        "Narita Express takes 53m to Tokyo Station; Haneda Monorail takes 18m to Hamamatsucho.\n"
        "- Kyoto Day Trips: Shinkansen Nozomi from Tokyo to Kyoto takes 2h15m (~$95 one-way); "
        "combine Fushimi Inari, Kiyomizu-dera, and Gion walking route.\n"
        "- Paris & London: Navigo Easy pass for Paris Metro zones 1-3; Eurostar St Pancras to "
        "Gare du Nord takes 2h16m city-center to city-center; book Louvre 2 weeks ahead.\n"
        "- Budget Allocation Rule: Reserve ~40% of total trip budget for flights, ~35% for "
        "lodging, and ~25% for local dining, transit, and museum admissions.\n"
    )
    # Repeat handbook sections to simulate ~5,500 tokens of uncached static prompt bloat
    sections = [
        f"[Handbook Appendix #{idx:02d}]\n{base_block}" for idx in range(1, 36)
    ]
    return "\n\n".join(sections)


def run_standalone_budget_helper(
    tower: TokenControlTower,
    destination: str = "Tokyo",
) -> tuple[str, dict[str, int]]:
    """Demonstrates `@track_tokens` on a standalone Python helper outside ADK."""

    @track_tokens(
        app_name="currency_tip_helper",
        model_name="gemini-3.8-flash",
        tracker=tower.tracker,
    )
    def _summarize_budget_tips(city: str) -> tuple[str, dict[str, int]]:
        summary = f"Quick budget checklist prepared for {city}."
        usage = {
            "input_tokens": 140,
            "cached_tokens": 420,
            "output_tokens": 85,
            "thinking_tokens": 0,
        }
        return summary, usage

    return _summarize_budget_tips(destination)
