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

"""Call-level retry policy: deadlines, stalls and transient transport errors.

HTTP 408/429/5xx retries live in the SDK (``core.clients.RETRY_OPTIONS``);
this module only covers errors that never reach an HTTP status.
"""

import contextvars
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextvars import ContextVar
from typing import TypeVar

from genmedia4commerce.core.errors import DeadlineExceeded

T = TypeVar("T")

__all__ = [
    "CANCEL",
    "EMPTY_RESULT_ATTEMPTS",
    "STALL_RETRIES",
    "TRANSPORT_ATTEMPTS",
    "ContextPool",
    "Deadline",
    "backoff_seconds",
    "is_transient",
]

TRANSPORT_ATTEMPTS = 4
EMPTY_RESULT_ATTEMPTS = 2
# A call that gets no answer within its deadline (a stall: replays of the
# same request answer normally) is asked again with a fresh deadline.
STALL_RETRIES = 1

_TRANSIENT_MARKERS = (
    "connection aborted",
    "connection reset",
    "econnreset",
    "eaddrnotavail",
    "broken pipe",
    "remote end closed",
    "server disconnected",
    "connection refused",
    "temporarily unavailable",
    "timed out",
    # The video model's "blocked due to copyright/recitation" (HTTP 400) is
    # not about the input: 3 of 3 re-sends of a refused spin passed.
    "recitation",
)

# Set by the runner for each variation. ``asyncio.to_thread`` copies it into
# the worker thread, so the model calls of a variation that was cancelled or
# timed out stop retrying instead of running to their own deadline.
CANCEL: ContextVar[threading.Event | None] = ContextVar("cancel", default=None)


class ContextPool(ThreadPoolExecutor):
    """A thread pool whose jobs run in a copy of the caller's context.

    Plain pool threads start with an empty context, so ``CANCEL`` would not
    reach the parallel judge and classifier calls; this carries it over.
    """

    def submit(self, fn, /, *args, **kwargs):
        """Run ``fn`` in a worker with the submitting thread's context."""
        context = contextvars.copy_context()
        return super().submit(context.run, fn, *args, **kwargs)


class Deadline:
    """A wall-clock budget measured with ``time.monotonic``.

    ``cancel`` (default: the current ``CANCEL``) ends the budget early once
    it is set.
    """

    def __init__(self, seconds: float, cancel: threading.Event | None = None):
        """Start a budget of ``seconds`` from now."""
        self.seconds = seconds
        self._end = time.monotonic() + seconds
        self.cancel = cancel if cancel is not None else CANCEL.get()

    def restart(self) -> None:
        """Start the same budget again from now (a call asked again)."""
        self._end = time.monotonic() + self.seconds

    @property
    def remaining(self) -> float:
        """Seconds left (never negative)."""
        return max(0.0, self._end - time.monotonic())

    @property
    def remaining_ms(self) -> int:
        """Milliseconds left as an HTTP timeout (at least one second)."""
        return max(1000, int(self.remaining * 1000))

    @property
    def expired(self) -> bool:
        """Whether the budget is used up."""
        return self.remaining <= 0

    @property
    def cancelled(self) -> bool:
        """Whether the ``cancel`` event is set."""
        return self.cancel is not None and self.cancel.is_set()

    def check(self) -> None:
        """Raise ``DeadlineExceeded`` when cancelled or the budget is used up."""
        if self.cancelled:
            raise DeadlineExceeded("cancelled")
        if self.expired:
            raise DeadlineExceeded(f"deadline of {self.seconds:g}s exceeded")

    def run(self, fn: Callable[[], T], what: str = "call") -> T:
        """Run ``fn`` in a helper thread and wait at most the remaining budget.

        HTTP read timeouts only fire when no bytes arrive, so a long model
        call that keeps the connection alive can outlive its deadline. This
        bounds the wall clock: past the budget the call is given up
        (``DeadlineExceeded``); the orphaned request finishes on its own and
        its result is discarded.
        """
        future: Future = Future()
        context = contextvars.copy_context()

        def worker() -> None:
            try:
                future.set_result(context.run(fn))
            except BaseException as exc:
                future.set_exception(exc)  # re-raised in the waiting thread

        threading.Thread(
            target=worker, name=f"deadline-{what}", daemon=True
        ).start()
        try:
            return future.result(timeout=max(self.remaining, 1.0))
        except FutureTimeout as exc:
            raise DeadlineExceeded(
                f"{what}: no answer within {self.seconds:g}s"
            ) from exc

    def sleep(self, seconds: float) -> bool:
        """Sleep ``seconds``; False (at once or on cancel) when out of time."""
        if seconds >= self.remaining or self.cancelled:
            return False
        if self.cancel is None:
            time.sleep(seconds)
            return True
        return not self.cancel.wait(seconds)


def is_transient(exc: BaseException) -> bool:
    """Return True for connection-level failures worth retrying."""
    if isinstance(exc, DeadlineExceeded):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def backoff_seconds(attempt: int) -> float:
    """Sleep before retry ``attempt`` (1-based): 1, 2, 4 seconds."""
    return float(2 ** (attempt - 1))
