# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""An investigation's stage rows: what it is doing now, for its live view.

The dashboard calls a running investigation stalled when its newest row is
older than 30 minutes (`STALE_AFTER_MS` in ui/web/lib/investigation-state.ts).
Some steps make one long call that reports nothing until it returns: the eval
service judging a page, a clustering or merge call, verification. While one
runs, `StageReporter.keep_alive` re-writes the newest report, so a working
investigation keeps showing it is alive. A process that dies stops writing at
once; one wedged in a call that never returns stops after
`KEEP_ALIVE_LIMIT_SECONDS`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING

from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.investigations.models import (
    InvestigationCancelledError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ambient_quality_agent.core.session_state import ADKStateLike
    from ambient_quality_agent.tools.investigations.models import (
        StageProgress,
    )

PROGRESS_INTERVAL_SECONDS = 300
"""Least time between two stage rows that are not a stage's first or a page's
last. Each row is a BigQuery load job, and the events table takes 1,500 of those
a day across every investigation in the deployment. Five minutes still writes
several rows inside the 30 minutes the dashboard waits before it calls an
investigation stalled."""

KEEP_ALIVE_LIMIT_SECONDS = 4 * 60 * 60
"""Longest `keep_alive` re-writes a row the node itself has not reported anew.
Longer than the slowest single model call (four attempts of up to 45 minutes,
`genai_json.LONG_CALL_TIMEOUT_MS`), so working investigations are never cut
off, and short enough that a wedged one reads stalled within hours, having
spent at most this long's worth of re-writes of the daily load-job quota."""

_KEEP_ALIVE_CHECK_SECONDS = 60
"""How often `keep_alive` checks whether a row is due. It sleeps on the wall
clock rather than waiting out the whole interval, so a row written meanwhile
pushes the next one back, and a newest row is at most this much later than
due."""


class StageReporter:
    """Writes an investigation's stage rows and knows when the next is due.

    Safe to call from the node's thread while `keep_alive` runs: both write
    under one lock, which is held across the write so two rows never race.
    """

    def __init__(self, state: ADKStateLike) -> None:
        """Binds the rows to an investigation.

        Args:
            state: Workflow state carrying the ``investigation_id`` to write
                under.
        """
        self._state = state
        self._lock = threading.Lock()
        self._latest: StageProgress | None = None
        self._reported: StageProgress | None = None
        self._written_at = time.monotonic()
        self._reported_at = self._written_at

    @property
    def latest(self) -> StageProgress | None:
        """The newest row written, if any."""
        return self._latest

    def write(self, progress: StageProgress) -> None:
        """Writes `progress` as the newest row, whenever it was last written.

        Args:
            progress: The stage and its counts, as the row will show them.
        """
        with self._lock:
            self._report(progress)
            self._write(progress)

    def write_if_due(self, progress: StageProgress) -> None:
        """Writes `progress` if `PROGRESS_INTERVAL_SECONDS` passed since the
        last row, counting one `keep_alive` may be writing right now.

        A report that is not due is still the newest: the next `keep_alive`
        re-write carries it, and it counts as the node reporting anew.

        Args:
            progress: The stage and its counts, as the row will show them.
        """
        with self._lock:
            self._report(progress)
            if self._is_due():
                self._write(progress)

    def _is_due(self) -> bool:
        return time.monotonic() - self._written_at >= PROGRESS_INTERVAL_SECONDS

    def _report(self, progress: StageProgress) -> None:
        self._reported = progress
        self._reported_at = time.monotonic()

    def _write(self, progress: StageProgress) -> None:
        self._latest = progress
        WorkflowState.record_stage_event(self._state, progress)
        self._written_at = time.monotonic()

    def _rewrite_if_due(self) -> None:
        with self._lock:
            silent = time.monotonic() - self._reported_at
            wedged = silent >= KEEP_ALIVE_LIMIT_SECONDS
            if self._reported is not None and self._is_due() and not wedged:
                self._write(self._reported)

    @contextlib.contextmanager
    def keep_alive(self) -> Iterator[None]:
        """Re-writes the newest report each interval while the block runs.

        Nothing is re-written before the first report, a row written inside
        the block pushes the next re-write back, and nothing is re-written
        once `KEEP_ALIVE_LIMIT_SECONDS` pass without the node reporting anew,
        or once a re-write finds the investigation cancelled.
        Leaving the block waits for a re-write in progress, so no row of this
        stage lands after the next stage's.
        """
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(_KEEP_ALIVE_CHECK_SECONDS):
                try:
                    self._rewrite_if_due()
                except InvestigationCancelledError:
                    # Nothing to keep alive: the node's own next write raises
                    # the same error and stops the investigation.
                    return

        thread = threading.Thread(
            target=beat, name="stage-keep-alive", daemon=True
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
