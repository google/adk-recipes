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

"""First-party ADK Grounding integrations (Google Search & Google Maps Grounding).

Because Gemini API rejects mixing built-in grounding tools (`google_search`,
`google_maps_grounding`) in the same `tools` declaration list as custom Python
FunctionTools on a single Agent, each built-in grounding tool lives on its own
dedicated specialist sub-agent and is invoked via an async tool wrapper.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import NamedTuple
from urllib.parse import quote_plus

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import google_maps_grounding, google_search
from google.genai import types

from app.app_utils.markdown import plain_text
from app.app_utils.models import grounding_model
from app.app_utils.timing import log_latency
from app.context_cards import record_context_card

logger = logging.getLogger(__name__)


async def _run_tool(
    service: str,
    work: Awaitable[str],
    *,
    on_error: Callable[[Exception], str] | None = None,
) -> str:
    try:
        return await work
    except Exception as exc:
        logger.warning("%s tool call failed", service, exc_info=True)
        if on_error:
            return on_error(exc)
        return f"{service} could not complete the request right now."


def _context_card_recorder(name: str) -> Callable[..., None]:
    return partial(
        record_context_card,
        kind=name,
        brand_icon=name,
        surface_id=f"a2ui-ctx-{name}",
    )


GROUNDING_MODEL = grounding_model()

# A grounded answer is a lookup, not a reasoning task: minimal thinking keeps the
# specialist's round trip, and so the voice's wait for its result, short. A model
# set in GROUNDING_MODEL must accept thinking levels, MINIMAL included.
_SPECIALIST_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(
        thinking_level=types.ThinkingLevel.MINIMAL
    )
)

_search_specialist = Agent(
    name="google_search_specialist",
    model=Gemini(model=GROUNDING_MODEL),
    instruction=(
        "You are a fast search specialist. Answer the user's query accurately using "
        "Google Search grounding. Return a concise, plain-text spoken summary (2-3 sentences max) "
        "with no markdown or URLs."
    ),
    tools=[google_search],
    generate_content_config=_SPECIALIST_CONFIG,
)

_maps_specialist = Agent(
    name="google_maps_specialist",
    model=Gemini(model=GROUNDING_MODEL),
    instruction=(
        "You are a location and places specialist using Google Maps grounding. "
        "Provide concise spoken answers with place names, ratings, distances, and open status "
        "in plain text without markdown or raw URLs."
    ),
    tools=[google_maps_grounding],
    generate_content_config=_SPECIALIST_CONFIG,
)

_USER = "voice_user"
_grounding_session_service = InMemorySessionService()
_runners = {
    agent.name: Runner(
        agent=agent,
        app_name=agent.name,
        session_service=_grounding_session_service,
    )
    for agent in (_search_specialist, _maps_specialist)
}


class Grounded(NamedTuple):
    """A specialist's spoken answer and the sources it was grounded on.

    Each source has `title` and `url`; web sources add `domain`, places add `summary`.
    """

    text: str
    sources: list[dict[str, str]]


def _source(chunk: types.GroundingChunk) -> dict[str, str] | None:
    if (web := chunk.web) and web.uri:
        title = web.title or web.domain or ""
        return {"title": title, "url": web.uri, "domain": web.domain or title}
    if (place := chunk.maps) and place.uri:
        return {
            "title": place.title or "",
            "url": place.uri,
            "summary": place.text or "",
        }
    return None


async def _run_specialist(agent: Agent, prompt: str) -> Grounded:
    """One question to a specialist, in a throwaway session deleted afterwards."""
    session = await _grounding_session_service.create_session(
        app_name=agent.name, user_id=_USER
    )
    msg = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    chunks: list[str] = []
    sources: dict[str, dict[str, str]] = {}
    try:
        with log_latency(
            "specialist", specialist=agent.name, model=GROUNDING_MODEL
        ):
            async for event in _runners[agent.name].run_async(
                user_id=_USER,
                session_id=session.id,
                new_message=msg,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            chunks.append(part.text)
                metadata = event.grounding_metadata
                for chunk in (
                    metadata.grounding_chunks if metadata else None
                ) or []:
                    if source := _source(chunk):
                        sources.setdefault(source["url"], source)
    finally:
        await _grounding_session_service.delete_session(
            app_name=agent.name, user_id=_USER, session_id=session.id
        )
    # Specialists sometimes answer in Markdown anyway; the voice and the card want prose.
    return Grounded(
        plain_text(" ".join(chunks)) or "No results found.",
        list(sources.values()),
    )


def _search_url(base: str, terms: str) -> str:
    return f"{base}{quote_plus(terms)}"


async def search_web_grounded(query: str) -> str:
    """Search the live web using first-party ADK Google Search grounding.

    Use this for quick answers you can say in a sentence or two: current news, weather,
    library versions, documentation lookups, or a single fact. Work that takes several steps
    or produces something to keep (an itinerary, a plan, a report) is a `start_thread`.

    Args:
        query: The search query to look up on Google Search.

    Returns:
        Grounded spoken answer from Google Search.
    """
    return await _run_tool(
        "Google Search",
        _search_web(query),
        on_error=lambda _exc: (
            f"Google Search grounding could not complete the request for {query} right now."
        ),
    )


async def _search_web(query: str) -> str:
    answer, sources = await _run_specialist(_search_specialist, query)
    by_domain: dict[str, dict[str, str]] = {}
    for source in sources:
        by_domain.setdefault(source.get("domain") or source["url"], source)
    _context_card_recorder("google_search")(
        title="Search",
        subtitle=query,
        bullets=[answer],
        items=list(by_domain.values())[:5],
        meta={
            "query": query,
            "search_url": _search_url(
                "https://www.google.com/search?q=", query
            ),
        },
    )
    return answer


async def search_maps_grounded(query: str, near_location: str) -> str:
    """Search places, coffee shops, offices, directions, or local businesses using first-party ADK Google Maps grounding.

    Args:
        query: What place or route to look for (e.g. 'quiet coffee shop with wifi', 'restaurants open now').
        near_location: City, neighborhood, or address the user gave. Ask them if they
            haven't said where.

    Returns:
        Grounded spoken places summary from Google Maps.
    """
    return await _run_tool(
        "Google Maps",
        _search_maps(query, near_location),
        on_error=lambda _exc: (
            f"Google Maps grounding could not complete the request for {query} near "
            f"{near_location} right now."
        ),
    )


async def _search_maps(query: str, near_location: str) -> str:
    answer, places = await _run_specialist(
        _maps_specialist, f"{query} near {near_location}"
    )
    maps_url = _search_url(
        "https://www.google.com/maps/search/", f"{query} {near_location}"
    )
    _context_card_recorder("google_maps")(
        title=f"Maps · {near_location}",
        subtitle=query,
        bullets=[answer],
        items=[
            {
                "name": p["title"],
                "url": p["url"],
                "summary": p.get("summary", ""),
            }
            for p in places[:5]
        ],
        meta={
            "query": query,
            "near_location": near_location,
            "maps_url": maps_url,
        },
    )
    return answer
