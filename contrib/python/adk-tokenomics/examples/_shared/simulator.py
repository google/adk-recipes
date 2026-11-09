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
Offline Trace Simulator for `run_travel_demo.py --simulate` and Automated Tests.

Replays the same 3-day Tokyo trip conversation ("Plan a 3-day Tokyo trip under $2,000")
through the real `ADKTokenomicsPlugin` callbacks on both `travel_unoptimized` and
`travel_optimized` so users and CI can inspect traces in <1 second without cloud keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adk_tokenomics_plugin import ADKTokenomicsPlugin, TokenControlTower
from examples._shared.travel_tools import (
    get_city_weather,
    search_flights,
    search_hotels,
)


@dataclass
class _SimFunctionCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SimPart:
    text: str | None = None
    function_call: _SimFunctionCall | None = None


@dataclass
class _SimContent:
    role: str = "model"
    parts: list[_SimPart] = field(default_factory=list)


@dataclass
class _SimCandidate:
    content: _SimContent


@dataclass
class _SimUsage:
    prompt_token_count: int
    cached_content_token_count: int
    candidates_token_count: int
    thoughts_token_count: int = 0


@dataclass
class _SimLlmRequest:
    model: str
    contents: list[Any] = field(default_factory=list)


@dataclass
class _SimLlmResponse:
    model_version: str
    usage_metadata: _SimUsage
    candidates: list[_SimCandidate] = field(default_factory=list)
    partial: bool = False


@dataclass
class _SimSession:
    id: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SimCallbackContext:
    session: _SimSession
    session_id: str
    invocation_id: str
    agent_name: str
    agent: Any
    user_content: Any


@dataclass
class _SimToolObj:
    name: str
    agent: Any | None = None


@dataclass
class _SimToolContext:
    state: dict[str, Any]
    session_id: str
    invocation_id: str
    agent_name: str
    agent: Any
    function_call_id: str


def simulate_tokyo_trip_session(
    *,
    app: Any,
    tower: TokenControlTower,
    optimized: bool,
    session_id: str,
) -> dict[str, Any]:
    """Runs a deterministic Tokyo trip conversation through real `ADKTokenomicsPlugin` hooks."""
    plugin: ADKTokenomicsPlugin = app.plugins[0]
    router = app.root_agent
    finder = router.tools[0].agent
    planner = router.sub_agents[0]
    state: dict[str, Any] = {}
    fc_seq = 0

    def _emit_turn(
        *,
        agent_obj: Any,
        inv_id: str,
        query: str,
        fresh: int,
        cached: int,
        out: int,
        think: int,
        reply: str,
        tools: list[tuple[_SimToolObj, dict[str, Any], Any]] | None = None,
        override_sid: str | None = None,
    ) -> None:
        nonlocal fc_seq
        eff_sid = override_sid or session_id
        sess = _SimSession(id=eff_sid, state=state)
        user_msg = {"role": "user", "parts": [{"text": query}]}
        cb_ctx = _SimCallbackContext(
            session=sess,
            session_id=eff_sid,
            invocation_id=inv_id,
            agent_name=agent_obj.name,
            agent=agent_obj,
            user_content=user_msg,
        )
        req = _SimLlmRequest(model=str(agent_obj.model), contents=[user_msg])
        plugin.before_model_callback(callback_context=cb_ctx, llm_request=req)

        parts: list[_SimPart] = []
        for t_obj, t_args, _ in tools or []:
            parts.append(
                _SimPart(
                    function_call=_SimFunctionCall(
                        name=t_obj.name, args=dict(t_args)
                    )
                )
            )
        if reply:
            parts.append(_SimPart(text=reply))

        resp = _SimLlmResponse(
            model_version=str(agent_obj.model),
            usage_metadata=_SimUsage(
                prompt_token_count=fresh + cached,
                cached_content_token_count=cached,
                candidates_token_count=out,
                thoughts_token_count=think,
            ),
            candidates=[_SimCandidate(content=_SimContent(parts=parts))],
        )
        plugin.after_model_callback(callback_context=cb_ctx, llm_response=resp)

        for t_obj, t_args, fn_call in tools or []:
            fc_seq += 1
            t_ctx = _SimToolContext(
                state=state,
                session_id=eff_sid,
                invocation_id=inv_id,
                agent_name=agent_obj.name,
                agent=agent_obj,
                function_call_id=f"fc_{session_id}_{fc_seq}",
            )
            plugin.before_tool_callback(
                tool=t_obj, tool_args=t_args, tool_context=t_ctx
            )
            res = fn_call() if callable(fn_call) else None
            plugin.after_tool_callback(
                tool=t_obj, tool_args=t_args, tool_context=t_ctx, result=res
            )

    q1 = "Plan a 3-day trip to Tokyo from SFO under $2,000."
    finder_tool_obj = _SimToolObj(name="flight_hotel_finder", agent=finder)

    if optimized:
        # Turn 1: Router loads `japan-guide` skill on demand + invokes `flight_hotel_finder` AgentTool
        def _run_opt_child() -> dict[str, Any]:
            _emit_turn(
                agent_obj=finder,
                inv_id="inv_child_1",
                query="Find round-trip SFO-Tokyo flights, hotels <= $200/night, and weather.",
                fresh=320,
                cached=1250,
                out=180,
                think=40,
                reply="ZIPAIR $590 nonstop; NOHGA Hotel Ueno $145/night; 14-20°C crisp & clear.",
                tools=[
                    (
                        _SimToolObj("search_flights"),
                        {
                            "destination": "Tokyo",
                            "origin": "SFO",
                            "trip_days": 3,
                        },
                        lambda: search_flights("Tokyo", "SFO", 3),
                    ),
                    (
                        _SimToolObj("search_hotels"),
                        {"city": "Tokyo", "max_nightly_usd": 200},
                        lambda: search_hotels("Tokyo", 200),
                    ),
                    (
                        _SimToolObj("get_city_weather"),
                        {"city": "Tokyo"},
                        lambda: get_city_weather("Tokyo"),
                    ),
                ],
                override_sid=f"child_{session_id}",
            )
            return {"status": "ok"}

        _emit_turn(
            agent_obj=router,
            inv_id="inv_1",
            query=q1,
            fresh=380,
            cached=1400,
            out=140,
            think=0,
            reply="",
            tools=[
                (
                    _SimToolObj("load_skill"),
                    {"skill_name": "japan-guide"},
                    lambda: "Loaded japan-guide skill.",
                ),
                (
                    finder_tool_obj,
                    {"request": "Tokyo flights, hotels <= $200, weather"},
                    _run_opt_child,
                ),
                (
                    _SimToolObj("transfer_to_agent"),
                    {"agent_name": "itinerary_planner"},
                    lambda: None,
                ),
            ],
        )
        # Turn 3: Itinerary synthesis on `gemini-4-argon` with cached prefix & bounded MEDIUM thinking
        _emit_turn(
            agent_obj=planner,
            inv_id="inv_1",
            query=q1,
            fresh=490,
            cached=2400,
            out=620,
            think=160,
            reply=(
                "3-Day Tokyo Itinerary ($1,205 total: $590 ZIPAIR + $435 NOHGA Ueno 3 nights + "
                "$180 Suica/food): Day 1 Asakusa & Ueno, Day 2 Shibuya & Meiji Shrine, Day 3 Shinjuku."
            ),
        )
        # Turn 4: Follow-up question with compacted + cached history
        q2 = "Can we add a half-day Kyoto bullet train trip and stay under $2,000?"
        _emit_turn(
            agent_obj=router,
            inv_id="inv_2",
            query=q2,
            fresh=340,
            cached=1850,
            out=210,
            think=0,
            reply=(
                "Yes! Round-trip Shinkansen adds ~$190, bringing your total to ~$1,395 — "
                "still $605 under your $2,000 budget."
            ),
        )
    else:
        # Unoptimized Baseline: Argon everywhere, 5,500-token inline manual, 0% cache, HIGH thinking, 4 uncompacted user turns
        def _run_unopt_child() -> dict[str, Any]:
            _emit_turn(
                agent_obj=finder,
                inv_id="inv_u_child_1",
                query="Find round-trip SFO-Tokyo flights, hotels <= $200/night, and weather.",
                fresh=1950,
                cached=0,
                out=210,
                think=880,
                reply="ZIPAIR $590 nonstop; NOHGA Hotel Ueno $145/night; 14-20°C crisp & clear.",
                tools=[
                    (
                        _SimToolObj("search_flights"),
                        {
                            "destination": "Tokyo",
                            "origin": "SFO",
                            "trip_days": 3,
                        },
                        lambda: search_flights("Tokyo", "SFO", 3),
                    ),
                    (
                        _SimToolObj("search_hotels"),
                        {"city": "Tokyo", "max_nightly_usd": 200},
                        lambda: search_hotels("Tokyo", 200),
                    ),
                    (
                        _SimToolObj("get_city_weather"),
                        {"city": "Tokyo"},
                        lambda: get_city_weather("Tokyo"),
                    ),
                ],
                override_sid=f"child_{session_id}",
            )
            return {"status": "ok"}

        _emit_turn(
            agent_obj=router,
            inv_id="inv_u1",
            query=q1,
            fresh=5800,
            cached=0,
            out=160,
            think=0,
            reply="",
            tools=[
                (
                    finder_tool_obj,
                    {"request": "Tokyo flights, hotels <= $200, weather"},
                    _run_unopt_child,
                ),
                (
                    _SimToolObj("transfer_to_agent"),
                    {"agent_name": "itinerary_planner"},
                    lambda: None,
                ),
            ],
        )
        _emit_turn(
            agent_obj=planner,
            inv_id="inv_u1",
            query=q1,
            fresh=3400,
            cached=0,
            out=640,
            think=1350,
            reply="3-Day Tokyo Itinerary ($1,205 total): Day 1 Asakusa, Day 2 Shibuya, Day 3 Shinjuku.",
        )
        _emit_turn(
            agent_obj=router,
            inv_id="inv_u2",
            query="Can we add a half-day Kyoto bullet train trip?",
            fresh=6450,
            cached=0,
            out=190,
            think=0,
            reply="Yes, Shinkansen round-trip adds ~$190 (~$1,395 total).",
        )
        _emit_turn(
            agent_obj=router,
            inv_id="inv_u3",
            query="What about airport train options from Narita vs Haneda?",
            fresh=7050,
            cached=0,
            out=210,
            think=0,
            reply="Haneda Monorail is 18 minutes; Narita Express is 53 minutes to Tokyo Station.",
        )
        _emit_turn(
            agent_obj=router,
            inv_id="inv_u4",
            query="Give me a final packing & Suica transit checklist.",
            fresh=7600,
            cached=0,
            out=230,
            think=950,
            reply="Add digital Suica to Apple/Google Wallet, pack walking shoes and a light jacket.",
        )

    tower.sink.flush()
    return {
        "session_id": session_id,
        "summary": tower.get_summary(),
        "turns": list(tower.tracker.turns_history),
    }
