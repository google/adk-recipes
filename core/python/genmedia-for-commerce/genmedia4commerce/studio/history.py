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

from google.adk.events import Event
from google.genai import types

from genmedia4commerce.studio import sessions

__all__ = ["STUDIO_PREFIX", "note"]

STUDIO_PREFIX = "[studio] "


def note(studio_id: str, text: str) -> bool:
    """Record a fact about the app as a ``[studio]`` line (no model run);
    whether it was stored. Failures are logged."""
    if not studio_id or not text:
        return False
    part = types.Part(text=STUDIO_PREFIX + text)
    event = Event(
        invocation_id=Event.new_id(),
        author="user",
        content=types.Content(role="user", parts=[part]),
    )
    return sessions.add_event(studio_id, event)
