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

"""Fallbacks taken: a step that went on with less than its full result.

A step that catches a failure and goes on without it (a close-up search
that failed, an upscale that kept the plain crop) calls ``report``.
Whoever needs to know, for example the preprocessing cache, which never
keeps such a result, runs the steps inside ``tracking`` and reads the
``Marker`` afterwards. The marker is a ContextVar holding one
shared object, so the reports of the threads and tasks the steps start
(``asyncio.to_thread``, ``core.retry.ContextPool``, ``asyncio.create_task``:
each copies the context, with the same marker) reach it. Outside
``tracking`` a report does nothing.
"""

import contextlib
import logging
import threading
from collections.abc import Iterator
from contextvars import ContextVar

__all__ = ["Marker", "report", "tracking"]

logger = logging.getLogger(__name__)


class Marker:
    """The fallbacks reported while it was tracking (thread-safe)."""

    def __init__(self) -> None:
        """Start with none."""
        self._reasons: list[str] = []
        self._lock = threading.Lock()

    def add(self, reason: str) -> None:
        """Record one fallback."""
        with self._lock:
            self._reasons.append(reason)

    @property
    def reasons(self) -> list[str]:
        """The fallbacks reported, in order."""
        with self._lock:
            return list(self._reasons)

    @property
    def ok(self) -> bool:
        """No fallback was reported."""
        return not self.reasons


_CURRENT: ContextVar[Marker | None] = ContextVar("degraded", default=None)


def report(reason: str) -> None:
    """Record a fallback in the marker tracking this context, if any."""
    marker = _CURRENT.get()
    if marker is not None:
        logger.info(f"fallback: {reason}")
        marker.add(reason)


@contextlib.contextmanager
def tracking(marker: Marker | None = None) -> Iterator[Marker]:
    """Collect the fallbacks reported in the block (and in the threads and
    tasks it starts) in ``marker`` (a new one by default). Not across a
    ``yield`` of an async generator: set it around each awaited step."""
    marker = Marker() if marker is None else marker
    token = _CURRENT.set(marker)
    try:
        yield marker
    finally:
        _CURRENT.reset(token)
