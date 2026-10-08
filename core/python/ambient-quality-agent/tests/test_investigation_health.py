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

"""The ambient-health verdict, at every boundary it has.

The rule is a pure function, so these are about the rule and not about
plumbing: which verdict wins when two could apply, when lateness can be judged
at all, and what the badge is told when a number would be a claim nobody made.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from ambient_quality_agent.tools.investigations import health

NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.UTC)


def run(
    *,
    hours_ago: float | None,
    status: str = "done",
    trigger: str = "ambient",
    evaluated: int = 100,
    passed: int = 90,
    run_id: str = "r",
) -> dict[str, Any]:
    finished = (
        (NOW - dt.timedelta(hours=hours_ago)).isoformat()
        if hours_ago is not None
        else None
    )
    return {
        "run_id": run_id,
        "status": status,
        "trigger_type": trigger,
        "finished_at": finished,
        "counters": {
            "traces_evaluated": evaluated,
            "traces_eval_passed": passed,
        },
    }


def daily(count: int, *, newest_hours_ago: float) -> list[dict[str, Any]]:
    """Builds a sequence of scheduled runs spaced 24 hours apart.

    Args:
        count: Number of run records to generate.
        newest_hours_ago: Age in hours of the newest run.

    Returns:
        List of generated run dictionaries ordered newest to oldest.
    """
    return [
        run(hours_ago=newest_hours_ago + 24 * i, run_id=f"r{i}")
        for i in range(count)
    ]


def insight(label: str, *, traces: int, status: str = "NEW") -> dict[str, Any]:
    return {
        "insight_id": f"ins-{label}",
        "label": label,
        "trace_count": traces,
        "status": status,
    }


def assess(runs, insights=()) -> health.Health:
    return health.assess(list(runs), list(insights), now=NOW)


# --------------------------------------------------------------------------- #
# Which verdict wins                                                           #
# --------------------------------------------------------------------------- #


def test_nothing_finished_is_never() -> None:
    verdict = assess([])
    assert verdict.verdict == health.NEVER
    assert "never" in verdict.reason.lower() or "scheduler" in verdict.reason


def test_nothing_finished_in_standalone_says_to_start_a_sweep() -> None:
    """Standalone has no scheduler, so blaming one would send the reader after
    something that does not exist."""
    verdict = health.assess([], [], now=NOW, standalone=True)

    assert verdict.verdict == health.NEVER
    assert "scheduler may never have fired" not in verdict.reason
    assert "Run investigation" in verdict.reason


def test_a_first_run_still_going_is_never_but_says_so() -> None:
    """ "Nothing has finished" and "the scheduler never fired" are different
    things to tell someone who has just deployed."""
    verdict = assess([run(hours_ago=None, status="running")])
    assert verdict.verdict == health.NEVER
    assert "running" in verdict.reason


def test_a_failed_last_run_is_failing() -> None:
    verdict = assess(
        [*daily(4, newest_hours_ago=1)[1:], run(hours_ago=0.5, status="failed")]
    )
    assert verdict.verdict == health.FAILING


def test_failing_outranks_overdue() -> None:
    """A loop that is turning and erroring needs attention now; that it is also
    late is the less useful half of the sentence."""
    runs = daily(4, newest_hours_ago=200)
    runs[-1] = run(hours_ago=200, status="failed", run_id="last")

    assert assess(runs).verdict == health.FAILING


def test_a_run_in_flight_suppresses_overdue() -> None:
    """Something is happening right now, which is what the badge is asked."""
    runs = [
        *daily(4, newest_hours_ago=200),
        run(hours_ago=None, status="running"),
    ]

    verdict = assess(runs)

    assert verdict.verdict == health.WATCHING
    assert "running now" in verdict.reason


def test_on_schedule_is_watching() -> None:
    verdict = assess(daily(5, newest_hours_ago=2))
    assert verdict.verdict == health.WATCHING
    assert verdict.max_age_hours == pytest.approx(48.0)


def test_late_by_more_than_the_factor_is_overdue() -> None:
    verdict = assess(daily(5, newest_hours_ago=49))
    assert verdict.verdict == health.OVERDUE
    assert "49h ago" in verdict.reason


def test_late_but_inside_the_factor_is_still_watching() -> None:
    """Two intervals, not one: a run that starts on time still takes time to
    finish. At one interval a healthy daily deployment is amber every day, and
    a badge that is always amber is one nobody reads."""
    assert assess(daily(5, newest_hours_ago=47)).verdict == health.WATCHING


# --------------------------------------------------------------------------- #
# When lateness can be judged at all                                           #
# --------------------------------------------------------------------------- #


def test_too_few_runs_to_know_the_rhythm_is_not_overdue() -> None:
    """A deployment that has run twice has no rhythm to be late against, and a
    red badge on a healthy new install is worse than no judgement."""
    verdict = assess(daily(2, newest_hours_ago=500))

    assert verdict.verdict == health.WATCHING
    assert verdict.max_age_hours is None
    assert "too few" in verdict.reason.lower()


def test_the_interval_is_the_median_not_the_mean() -> None:
    """One outage should not redefine normal, and the mean is exactly what an
    outage moves."""
    stamps = [0, 24, 48, 500, 524]  # one long gap in the middle
    runs = [run(hours_ago=h, run_id=f"r{h}") for h in stamps]

    # Newest finished 0h ago; gaps are 24, 24, 452, 24 -> median 24.
    assert assess(runs).max_age_hours == pytest.approx(48.0)


def test_manual_runs_do_not_set_the_rhythm() -> None:
    """Someone clicking the button twice is not a schedule, and counting it as
    one would make a genuinely stalled loop look healthy."""
    runs = [
        *daily(3, newest_hours_ago=200),
        run(hours_ago=1, trigger="manual", run_id="m1"),
        run(hours_ago=2, trigger="manual", run_id="m2"),
    ]

    verdict = assess(runs)

    assert verdict.verdict == health.OVERDUE
    # The badge still reports the newest run, manual or not.
    assert verdict.last_run["trigger_type"] == "manual"
    # But the lateness is measured against the scheduled one.
    assert verdict.last_scheduled_finish is not None


def test_custom_runs_do_not_set_the_rhythm() -> None:
    """Verify custom runs do not update scheduled cadence intervals in health assessments."""
    runs = [
        *daily(3, newest_hours_ago=200),
        run(hours_ago=1, trigger="custom", run_id="c1"),
        run(hours_ago=2, trigger="custom", run_id="c2"),
    ]

    verdict = assess(runs)

    assert verdict.verdict == health.OVERDUE
    assert verdict.last_scheduled_finish is not None


# --------------------------------------------------------------------------- #
# What the badge is told                                                       #
# --------------------------------------------------------------------------- #


def test_a_run_that_evaluated_nothing_has_no_pass_rate() -> None:
    """Null, not zero: 0% is a claim about quality that the run did not make."""
    verdict = assess([run(hours_ago=1, evaluated=0, passed=0)])

    assert verdict.last_run["sessions"] is None
    assert verdict.last_run["sessions_passed"] is None
    assert verdict.last_run["pass_rate"] is None


def test_a_pass_rate_is_reported_as_a_fraction() -> None:
    verdict = assess([run(hours_ago=1, evaluated=200, passed=150)])
    assert verdict.last_run["pass_rate"] == pytest.approx(0.75)
    assert verdict.last_run["sessions"] == 200


def test_open_findings_exclude_resolved_ones() -> None:
    verdict = assess(
        daily(3, newest_hours_ago=1),
        [
            insight("small", traces=5),
            insight("big", traces=500),
            insight("gone", traces=900, status="RESOLVED"),
        ],
    )

    assert verdict.open_findings == 2
    assert verdict.worst_finding["label"] == "big"


def test_no_findings_is_zero_and_nothing_worst() -> None:
    verdict = assess(daily(3, newest_hours_ago=1))
    assert verdict.open_findings == 0
    assert verdict.worst_finding is None


def test_the_schedule_is_null_because_the_engine_cannot_read_it() -> None:
    """The cron lives in the Cloud Scheduler job and is never handed to the
    engine. The card falls back to `reason`, which is why the reason always
    says something about the rhythm."""
    verdict = assess(daily(4, newest_hours_ago=1))
    assert verdict.schedule is None
    assert verdict.reason


def test_the_payload_carries_every_field_the_badge_reads() -> None:
    payload = assess(
        daily(4, newest_hours_ago=1), [insight("x", traces=1)]
    ).to_payload()

    assert set(payload) == {
        "verdict",
        "reason",
        "schedule",
        "max_age_hours",
        "last_scheduled_finish",
        "last_run",
        "open_findings",
        "worst_finding",
    }


def test_an_unparseable_finish_time_does_not_crash_the_verdict() -> None:
    """A row written by an older engine should cost that row, not the badge."""
    runs = [
        *daily(4, newest_hours_ago=1),
        {"run_id": "bad", "status": "done", "finished_at": "not a date"},
    ]

    assert assess(runs).verdict == health.WATCHING
