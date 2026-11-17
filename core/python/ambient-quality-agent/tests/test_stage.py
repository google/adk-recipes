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

"""Tests for the stage rows a running investigation writes for its live view."""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Any

import pytest
from ambient_quality_agent.core import state as state_module
from ambient_quality_agent.core.nodes import _stage
from ambient_quality_agent.tools.investigations.models import StageProgress


class _Clock:
    """A `monotonic` the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Hand-moved clock, and a keep-alive that checks it almost at once."""
    clock = _Clock()
    monkeypatch.setattr(_stage.time, "monotonic", clock)
    monkeypatch.setattr(_stage, "_KEEP_ALIVE_CHECK_SECONDS", 0.01)
    return clock


def _stages(store: Any) -> list[dict[str, Any]]:
    return [
        json.loads(r["text"])
        for r in store.event_rows
        if r["source"] == "stage"
    ]


def _wait_for(predicate: Any) -> None:
    deadline = time.perf_counter() + 5
    while not predicate():
        assert time.perf_counter() < deadline, "the keep-alive never wrote"
        time.sleep(0.01)


def test_a_long_step_keeps_rewriting_its_stage(
    clock: _Clock, investigation_store: Any
) -> None:
    """A clustering call can outlast the 30 minutes after which the dashboard
    calls an investigation stalled, so while it runs its row is written again
    each interval."""
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})

    with reporter.keep_alive():
        reporter.write(StageProgress(stage="Clustering", findings=4))
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        _wait_for(lambda: len(investigation_store.event_rows) == 2)

    assert (
        _stages(investigation_store)
        == [{"stage": "Clustering", "findings": 4}] * 2
    )


def test_nothing_is_rewritten_before_the_interval_or_after_the_step(
    clock: _Clock, investigation_store: Any
) -> None:
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})

    with reporter.keep_alive():
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        time.sleep(0.05)  # Due, but nothing written yet to re-write.
        reporter.write(StageProgress(stage="Merge"))
        time.sleep(0.05)  # Written, but not due again.
    clock.now += _stage.PROGRESS_INTERVAL_SECONDS
    time.sleep(0.05)  # Due, but the step is over.

    assert _stages(investigation_store) == [{"stage": "Merge"}]


def test_a_failing_events_table_does_not_fail_the_step(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows are for the live view only: an investigation that cannot write
    them still finishes."""
    calls: list[str] = []

    def failing_writer(_state: Any, text: str, _source: str) -> None:
        calls.append(text)
        raise RuntimeError("load job quota exceeded")

    monkeypatch.setattr(state_module, "event_writer", failing_writer)
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})

    with reporter.keep_alive():
        reporter.write(StageProgress(stage="Clustering"))
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        _wait_for(lambda: len(calls) == 2)

    assert len(calls) == 2


def test_a_wedged_step_stops_being_kept_alive(
    clock: _Clock, investigation_store: Any
) -> None:
    """A call that never returns must not keep its investigation looking
    alive, or spend the daily load jobs, for as long as the process lives."""
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})

    with reporter.keep_alive():
        reporter.write(StageProgress(stage="Clustering"))
        interval = _stage.PROGRESS_INTERVAL_SECONDS
        while clock.now + interval < _stage.KEEP_ALIVE_LIMIT_SECONDS:
            clock.now += interval
            before = len(investigation_store.event_rows)
            _wait_for(
                lambda before=before: (
                    len(investigation_store.event_rows) > before
                )
            )
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        time.sleep(0.05)
        rows = len(investigation_store.event_rows)
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        time.sleep(0.05)

    assert len(investigation_store.event_rows) == rows
    # The node's own row, then one re-write an interval until it had been
    # silent the whole limit.
    assert rows == _stage.KEEP_ALIVE_LIMIT_SECONDS // (
        _stage.PROGRESS_INTERVAL_SECONDS
    )


def test_a_report_due_while_a_rewrite_is_in_flight_is_not_doubled(
    clock: _Clock, investigation_store: Any
) -> None:
    """`write_if_due` decides under the lock a re-write holds, so a report
    queued behind one finds the row fresh and writes nothing."""
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})
    reporter.write(StageProgress(stage="Session review", evaluated=1))
    clock.now += _stage.PROGRESS_INTERVAL_SECONDS
    reporter._rewrite_if_due()  # What the keep-alive does when due.

    reporter.write_if_due(StageProgress(stage="Session review", evaluated=2))

    assert [r["evaluated"] for r in _stages(investigation_store)] == [1, 1]


def test_a_rewrite_writes_the_newest_report_even_one_not_yet_written(
    clock: _Clock, investigation_store: Any
) -> None:
    """A report that lands just after a re-write is not due, but the next
    re-write carries it rather than the older count."""
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})
    reporter.write(StageProgress(stage="Session review", evaluated=0))
    clock.now += _stage.PROGRESS_INTERVAL_SECONDS
    reporter._rewrite_if_due()
    reporter.write_if_due(StageProgress(stage="Session review", evaluated=2))
    clock.now += _stage.PROGRESS_INTERVAL_SECONDS

    reporter._rewrite_if_due()

    assert [r["evaluated"] for r in _stages(investigation_store)] == [0, 0, 2]


def test_a_step_that_keeps_reporting_is_never_taken_for_wedged(
    clock: _Clock, investigation_store: Any
) -> None:
    """Reports that each land just after a re-write are never due themselves,
    but they show the step is working, so re-writes go on past the limit."""
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})
    reporter.write(StageProgress(stage="Session review", evaluated=0))
    steps = (
        _stage.KEEP_ALIVE_LIMIT_SECONDS // _stage.PROGRESS_INTERVAL_SECONDS + 2
    )

    for judged in range(1, steps + 1):
        clock.now += _stage.PROGRESS_INTERVAL_SECONDS
        reporter._rewrite_if_due()
        reporter.write_if_due(
            StageProgress(stage="Session review", evaluated=judged)
        )

    evaluated = [r["evaluated"] for r in _stages(investigation_store)]
    assert len(evaluated) == steps + 1
    assert evaluated[-1] == steps - 1


def test_no_row_is_written_outside_an_investigation(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node run with no investigation (a test harness, a local run) has
    nowhere to write its stage."""
    calls: list[str] = []
    monkeypatch.setattr(
        state_module,
        "event_writer",
        lambda _state, text, _source: calls.append(text),
    )

    _stage.StageReporter({}).write(StageProgress(stage="Clustering"))

    assert calls == []


def test_a_timed_out_write_frees_the_reporter(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event write gives up on a stalled load job with `TimeoutError`; the
    row is lost and logged, and the lock is free for the node's next write."""
    calls: list[str] = []

    def stalled_writer(_state: Any, text: str, _source: str) -> None:
        calls.append(text)
        raise concurrent.futures.TimeoutError

    monkeypatch.setattr(state_module, "event_writer", stalled_writer)
    reporter = _stage.StageReporter({"investigation_id": "investigation-1"})

    reporter.write(StageProgress(stage="Clustering"))
    reporter.write(StageProgress(stage="Verification"))

    assert len(calls) == 2
