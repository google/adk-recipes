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

"""Size limits of a Studio session (``studio.sessions``), so a studio whose
id is known cannot be made unusable by flooding it.

- A user's message is at most ``MAX_MESSAGE`` characters: the MCP tool,
  the REST route and the app's ADK run routes (``api.message_limit``)
  refuse a longer one (``TOO_LONG``), and the session service cuts one when
  it stores it (``capped``), for ``adk web`` and ``adk eval``, whose
  servers do not check.
- The ``[studio]`` notes (``studio.history``) are stored as objects whose
  names end in ``NOTE_SUFFIX``; only the latest ``MAX_NOTES`` are kept
  (``trim_notes``), the older ones are dropped, never the conversation.
"""

from __future__ import annotations

import logging

from google.adk.events import Event

__all__ = [
    "MAX_MESSAGE",
    "MAX_NOTES",
    "NOTE_SUFFIX",
    "TOO_LONG",
    "capped",
    "trim_notes",
]

logger = logging.getLogger(__name__)

#: Characters of one user message (as the REST API's agent body).
MAX_MESSAGE = 4000
#: Why a longer message is refused.
TOO_LONG = f"message: at most {MAX_MESSAGE} characters"
#: ``[studio]`` notes kept per session, the most recent ones.
MAX_NOTES = 200
#: The end of a note's object name (other events end in ``.json``).
NOTE_SUFFIX = ".note.json"


def capped(event: Event) -> Event:
    """``event`` with the user's typed text (its first text part) cut to
    ``MAX_MESSAGE`` characters; the parts the Studio plugin appends (the
    current studio, its images) are kept."""
    if event.author != "user" or not event.content:
        return event
    parts = event.content.parts or []
    first = next((p for p in parts if p.text is not None), None)
    if first is not None and len(first.text or "") > MAX_MESSAGE:
        logger.warning(
            "A user message of %d characters was cut to %d",
            len(first.text or ""),
            MAX_MESSAGE,
        )
        first.text = (first.text or "")[:MAX_MESSAGE]
    return event


def trim_notes(names: list[str]) -> tuple[list[str], list[str]]:
    """Event object names (in order) without the notes older than the
    latest ``MAX_NOTES``, and the dropped names."""
    notes = [n for n in names if n.endswith(NOTE_SUFFIX)]
    dropped = set(notes[: max(0, len(notes) - MAX_NOTES)])
    if not dropped:
        return names, []
    return [n for n in names if n not in dropped], sorted(dropped)
