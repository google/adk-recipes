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

"""What a Studio client can do, as plain dicts.

Two clients share these operations: the MCP App panel (``mcp/studio.py``,
in Gemini Enterprise) and the web app (``api/studio.py``).
Both show the studio the server owns (``studio.state``), send each user
action as an event, follow generations as jobs and talk to the Studio agent
in panel mode (``agent.run_agent_turn``).

Synchronous functions do blocking I/O: callers run them with
``asyncio.to_thread``. Errors are the ``core.errors`` types. Catalogue
thumbnails are data URLs for the panel (``inline``, the default) and
same-origin URLs for the web app (``studio.picks``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from google.adk.events import Event

from genmedia4commerce import tools
from genmedia4commerce.core.catalog_search import MAX_K
from genmedia4commerce.core.errors import InputError, StorageError
from genmedia4commerce.studio import agent, jobs, picks, quota, sessions
from genmedia4commerce.studio import history as sh
from genmedia4commerce.studio import media as sm
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio.events import narrate

__all__ = [
    "agent_turn",
    "catalog_search",
    "conversation",
    "event",
    "gallery",
    "job_start",
    "job_status",
    "previews",
    "state",
    "upload",
]

logger = logging.getLogger(__name__)

_CATALOG_RESULTS = 8
_SESSIONS = sessions.StudioSessionService()


# ---- the agent ---------------------------------------------------------------


def _with_state(payload: dict[str, Any], saved_at: float) -> dict[str, Any]:
    """``payload`` with the saved studio (``state``, ``updated_at``) when it
    was saved (``saved_at``) and can be read back, for the client to show."""
    if saved_at:
        try:
            saved = ss.saved_state(payload["studio_id"])
        except StorageError as exc:
            logger.warning("studio %s: %s", payload["studio_id"], exc)
        else:
            payload.update(state=saved["state"], updated_at=saved["updated_at"])
    return payload


async def agent_turn(message: str, studio_id: str = "") -> dict[str, Any]:
    """Run the Studio agent on one user message (a new studio when
    ``studio_id`` is ""): ``{studio_id, reply, patches, issued_at}``, plus
    the saved ``state`` and ``updated_at`` when the turn saved it."""
    sid, done = await agent.run_agent_turn(message, studio_id)
    payload = {
        "studio_id": sid,
        "reply": done.reply,
        "patches": done.patches,
        "issued_at": done.saved_at or time.time(),
    }
    return await asyncio.to_thread(_with_state, payload, done.saved_at)


def _user_text(ev: Event) -> str:
    """What the user typed: the first text part of a user event ("" for a
    ``[studio]`` fact). The studio summary and images the agent's plugin
    appends come after it."""
    parts = (ev.content.parts or []) if ev.content else []
    text = next((p.text for p in parts if p.text), "")
    return "" if text.startswith(sh.STUDIO_PREFIX.strip()) else text


def _agent_text(ev: Event) -> str:
    """The agent's final reply in an event ("" for tool calls, tool
    responses and thoughts)."""
    if not (ev.content and ev.is_final_response()):
        return ""
    texts = [p.text for p in ev.content.parts or [] if p.text and not p.thought]
    return " ".join(texts).strip()


async def conversation(studio_id: str) -> dict[str, Any]:
    """The chat of a studio, from its ADK session: ``{studio_id, messages:
    [{role: "user" | "agent", text}]}`` (no messages when it has none)."""
    session = await _SESSIONS.get_session(
        app_name=sessions.APP_NAME,
        user_id=sessions.PANEL_USER,
        session_id=studio_id,
    )
    messages: list[dict[str, str]] = []
    for ev in session.events if session else []:
        user = ev.author == "user"
        text = _user_text(ev) if user else _agent_text(ev)
        if text:
            messages.append({"role": "user" if user else "agent", "text": text})
    return {"studio_id": studio_id, "messages": messages}


# ---- the studio ----------------------------------------------------------------


def _job_finished(studio_id: str, value: Any) -> dict[str, Any]:
    """Record the generation the client followed, as its job reports it."""
    job_id = value.get("job_id") if isinstance(value, dict) else None
    if not (isinstance(job_id, str) and job_id):
        raise InputError("job_finished needs a job_id.")
    return ss.finish_job(studio_id, job_id, jobs.status(job_id))


def event(studio_id: str, action: Any) -> dict[str, Any]:
    """Apply one user action to the saved studio (an op of ``studio.state``,
    or ``{"job_finished": {"job_id"}}``) and note it for the agent:
    ``{state, updated_at}`` (and ``refs`` for an op)."""
    if isinstance(action, dict) and set(action) == {"job_finished"}:
        return _job_finished(studio_id, action["job_finished"])
    return ss.apply_action(studio_id, action)


def state(studio_id: str) -> dict[str, Any]:
    """The saved studio, with new preview URLs: ``{studio_id, saved,
    updated_at, state}``."""
    return ss.saved_state(studio_id)


def previews(refs: list[str], *, inline: bool = True) -> dict[str, Any]:
    """``{previews: {ref: browser URL}}`` for image refs."""
    return {"previews": sm.previews(refs, inline=inline)}


def gallery(*, inline: bool = True) -> dict[str, Any]:
    """The catalogue's preset ``models`` and ``spin`` sets, with
    thumbnails."""
    return picks.gallery(inline=inline)


def upload(
    image: str | bytes, filename: str = "", studio_id: str = ""
) -> dict[str, str]:
    """Save an uploaded image (base64, a data URL or bytes) for the studio:
    ``{ref, label, thumb, description}``."""
    if isinstance(image, bytes):
        return sm.save_image(image, filename, studio_id)
    return sm.save_upload(image, filename, studio_id)


async def catalog_search(
    query: str, audience: str = "", studio_id: str = "", *, inline: bool = True
) -> dict[str, Any]:
    """The products picker: the featured products for an empty ``query``,
    else the best matches, which are noted for the agent. ``{query, items,
    error}`` (``error`` None unless the search failed)."""
    query = query.strip()
    k = _CATALOG_RESULTS if query else MAX_K
    res = await tools.catalog_search(query=query, k=k, audience=audience)

    def shown() -> list[dict[str, str]]:
        rows = res.get("items") or []
        return [picks.app_item(r, inline=inline) for r in rows]

    items = await asyncio.to_thread(shown)
    if query:
        search = {"type": "search", "query": query, "items": items}
        await asyncio.to_thread(sh.note, studio_id, narrate(search) or "")
    return {"query": query, "items": items, "error": res.get("error")}


async def job_start(spec: dict[str, Any]) -> dict[str, Any]:
    """Start a generation through the cost guards (``studio.quota``):
    ``{job_id, status, capability}``. Raises ``InputError`` for a bad spec,
    a studio never saved, a generation still running in it or too many
    starts; ``BusyError`` when the studio could not be read or saved,
    ``StorageError`` when another stored document could not be read."""
    return await quota.start(spec)


def job_status(job_id: str) -> dict[str, Any]:
    """Status, progress and results so far of a generation."""
    return jobs.status(job_id)
