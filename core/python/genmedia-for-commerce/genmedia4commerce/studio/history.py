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

"""``[studio]`` lines: facts about the app in the Studio agent's session.

The agent's conversation is its ADK session (``studio.sessions``). The app,
finished jobs and panel actions add facts to it as user events whose text
starts with ``[studio]`` (uploads, picks, generations with their ``gs://``
URIs), so the next turn of the agent sees them.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait

from google.adk.events import Event
from google.genai import types

from genmedia4commerce.studio import sessions

__all__ = ["STUDIO_PREFIX", "flush", "note", "note_later"]

STUDIO_PREFIX = "[studio] "
_WRITERS = 4  # notes ``note_later`` writes at the same time
#: Writes the notes of ``note_later``; ``_PENDING`` are not written yet.
_WRITER = ThreadPoolExecutor(_WRITERS, thread_name_prefix="studio-note")
_PENDING: set[Future] = set()


def _event(text: str) -> Event:
    """A ``[studio]`` line as a user event, stamped now."""
    part = types.Part(text=STUDIO_PREFIX + text)
    return Event(
        invocation_id=Event.new_id(),
        author="user",
        content=types.Content(role="user", parts=[part]),
    )


def note(studio_id: str, text: str) -> bool:
    """Record a fact about the app as a ``[studio]`` line (no model run);
    whether it was stored. Failures are logged."""
    if not studio_id or not text:
        return False
    return sessions.add_event(studio_id, _event(text))


def note_later(studio_id: str, text: str) -> None:
    """``note`` written in the background, so the caller answers first.
    The event is stamped now: its time orders the notes. Failures are
    logged; ``flush`` waits for the writes."""
    if not studio_id or not text:
        return
    future = _WRITER.submit(sessions.add_event, studio_id, _event(text))
    _PENDING.add(future)
    future.add_done_callback(_PENDING.discard)


def flush(timeout: float = 10.0) -> bool:
    """Wait for the notes ``note_later`` is writing, at most ``timeout``
    seconds (the app's shutdown, tests); whether none is left."""
    return not wait(set(_PENDING), timeout).not_done
