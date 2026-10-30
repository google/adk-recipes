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

"""What one process already read of the ADK sessions (``studio.sessions``).

Event objects are create-only, so a parsed event read once never changes:
``SessionCache`` keeps them by object name for the sessions read most
recently, and the ``session.json`` paths known to exist. Both are bounded
(least recently used first out) and safe to use from several threads.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from google.adk.events import Event

__all__ = ["SessionCache"]

#: Sessions whose events a process keeps, and sessions it knows exist.
_CACHED_SESSIONS = 32
_KNOWN_SESSIONS = 1024


class SessionCache:
    """Parsed events by session folder and object name, and the
    ``session.json`` paths known to exist."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: OrderedDict[str, dict[str, Event]] = OrderedDict()
        self._known: OrderedDict[str, None] = OrderedDict()

    def _session(self, folder: str) -> dict[str, Event]:
        events = self._events.pop(folder, {})
        self._events[folder] = events  # the most recent, last
        while len(self._events) > _CACHED_SESSIONS:
            self._events.popitem(last=False)
        return events

    def events(self, folder: str, names: list[str]) -> dict[str, Event]:
        """The cached events among ``names``, all the session's objects;
        the others (deleted) are forgotten. Do not change them."""
        with self._lock:
            events = self._session(folder)
            kept = {n: events[n] for n in names if n in events}
            self._events[folder] = kept
            return dict(kept)

    def put(self, folder: str, name: str, event: Event) -> None:
        """Keep a parsed event object (never changed afterwards)."""
        with self._lock:
            self._session(folder)[name] = event

    def known(self, path: str) -> bool:
        """Whether the session at ``path`` is known to exist."""
        with self._lock:
            return path in self._known

    def mark(self, path: str) -> None:
        """Remember that the session at ``path`` exists."""
        with self._lock:
            self._known.pop(path, None)
            self._known[path] = None
            while len(self._known) > _KNOWN_SESSIONS:
                self._known.popitem(last=False)

    def forget(self, folder: str, path: str) -> None:
        """Forget a deleted session: its events and its ``path``."""
        with self._lock:
            self._events.pop(folder, None)
            self._known.pop(path, None)
