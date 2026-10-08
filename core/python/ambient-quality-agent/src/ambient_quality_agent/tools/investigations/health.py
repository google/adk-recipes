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

"""Is the ambient loop turning? One verdict, and the reason for it.

The homepage's first answer, and the one surface whose whole job is to be true:
"your scheduler has never fired" is not a sentence to leave to a model's choice
of words. So this is a rule, written here in prose and in code, over data that
already exists -- the investigation records and the insight list. It reads no
new storage.

**The four verdicts, in the order they are decided.** Order is the design: a
loop that is turning and failing is worse news than one that is merely late, so
`failing` outranks `overdue`.

1. ``failing`` -- the most recent finished run ended in `FAILED`. The loop is
   turning and producing errors, which needs attention now whether or not the
   schedule is being kept.
2. ``never`` -- nothing has ever finished. Either the scheduler has not fired
   or every run so far is still in flight; the reason says which. A standalone
   run has no scheduler, so there the reason says to start a sweep instead.
3. ``overdue`` -- the newest finished run is older than `OVERDUE_FACTOR` times
   the interval this deployment actually runs at, and nothing is in flight.
4. ``watching`` -- none of the above.

**Where the expected interval comes from.** Not from configuration: the cron
lives in the Cloud Scheduler job (`var.investigation_schedule`) and is never
handed to the engine, so this service cannot read its own schedule. It is
inferred instead, from the gaps between recent scheduled runs -- the median, so
one long outage does not redefine "normal".

That has a consequence worth stating: **with fewer than
`MIN_INTERVALS_FOR_OVERDUE` scheduled runs to measure, `overdue` is not
reachable at all** and `max_age_hours` is null. A deployment that has run twice
has no established rhythm to be late against, and inventing a threshold would
put a red badge on a healthy new install. `watching` is the honest answer there,
and the reason says the rhythm is not known yet.

**Anything in flight counts as life.** A pending or running sweep means the loop
is turning right now, so it suppresses `overdue` regardless of when the last one
finished.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import statistics
from typing import Any

from ambient_quality_agent.tools.investigations.models import (
    NON_AMBIENT_TRIGGER_TYPES,
    RunStatus,
)

WATCHING = "watching"
OVERDUE = "overdue"
NEVER = "never"
FAILING = "failing"

OVERDUE_FACTOR = 2.0
"""How many expected intervals may pass before the loop counts as late.

Two, not one: a run that starts on time still takes time to finish, and
scheduler jitter is normal. At 1.0 a healthy daily deployment would spend part
of every day amber, which trains people to ignore the badge -- the failure mode
that matters most for a signal like this."""

MIN_INTERVALS_FOR_OVERDUE = 3
"""Scheduled runs needed before lateness can be judged: three runs, so there
are two gaps and a median that is not a single measurement."""

IN_FLIGHT = frozenset({RunStatus.PENDING, RunStatus.RUNNING})


@dataclasses.dataclass(frozen=True)
class Health:
    """The verdict and everything the badge renders beside it."""

    verdict: str
    reason: str
    schedule: str | None
    max_age_hours: float | None
    last_scheduled_finish: str | None
    last_run: dict[str, Any] | None
    open_findings: int
    worst_finding: dict[str, str] | None

    def to_payload(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def assess(
    runs: list[dict[str, Any]],
    insights: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    schedule: str | None = None,
    standalone: bool = False,
) -> Health:
    """Computes deployment health verdict from investigation runs and open insights.

    Evaluates finished runs, active sweeps, and open findings against expected intervals.

    Args:
        runs: List of investigation run dictionaries.
        insights: List of insight dictionaries.
        now: Optional reference timestamp (defaults to UTC now).
        schedule: Optional cron schedule expression string.
        standalone: Whether this is a standalone run, which has no scheduler
            and sweeps only when someone starts one.

    Returns:
        Health instance containing verdict, reason, and summary metrics.
    """
    now = now or dt.datetime.now(tz=dt.UTC)
    finished = _list_finished_runs(runs)
    scheduled = [
        r
        for r in finished
        if r.get("trigger_type") not in NON_AMBIENT_TRIGGER_TYPES
    ]
    in_flight = [r for r in runs if str(r.get("status") or "") in IN_FLIGHT]

    expected_hours = _compute_expected_interval_hours(scheduled)
    max_age = expected_hours * OVERDUE_FACTOR if expected_hours else None
    last_scheduled_finish = (
        _parse_finished_at(scheduled[-1]) if scheduled else None
    )
    open_findings, worst = _summarize_open_findings(insights)

    common: dict[str, Any] = {
        "schedule": schedule,
        "max_age_hours": round(max_age, 1) if max_age else None,
        "last_scheduled_finish": last_scheduled_finish.isoformat()
        if last_scheduled_finish
        else None,
        "last_run": _format_last_run(finished[-1]) if finished else None,
        "open_findings": open_findings,
        "worst_finding": worst,
    }

    if not finished:
        if in_flight:
            reason = f"{len(in_flight)} sweep(s) are running, but none has finished yet."
        elif standalone:
            reason = (
                "No sweep has ever finished. A standalone run has no scheduler: "
                "select Run investigation to start one."
            )
        else:
            reason = "No sweep has ever finished. The scheduler may never have fired."
        return Health(verdict=NEVER, reason=reason, **common)

    if str(finished[-1].get("status") or "") == RunStatus.FAILED:
        return Health(
            verdict=FAILING,
            reason="The most recent sweep failed.",
            **common,
        )

    if in_flight:
        return Health(
            verdict=WATCHING,
            reason=f"{len(in_flight)} sweep(s) running now.",
            **common,
        )

    if max_age is None:
        return Health(
            verdict=WATCHING,
            reason=(
                "Sweeps are finishing. Too few scheduled runs so far to know "
                "how often to expect them, so lateness is not being judged yet."
            ),
            **common,
        )

    age_hours = (
        (now - last_scheduled_finish).total_seconds() / 3600
        if last_scheduled_finish
        else None
    )
    if age_hours is not None and age_hours > max_age:
        return Health(
            verdict=OVERDUE,
            reason=(
                f"The last scheduled sweep finished {age_hours:.0f}h ago, and "
                f"they usually arrive every {expected_hours:.0f}h."
            ),
            **common,
        )
    return Health(
        verdict=WATCHING,
        reason=f"Scheduled sweeps are arriving about every {expected_hours:.0f}h.",
        **common,
    )


def _list_finished_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filters and orders runs that reached a terminal state.

    Orders runs by parsed timestamps to ensure consistent sorting across timezone
    representations.

    Args:
        runs: List of run dictionaries.

    Returns:
        Terminal run dictionaries sorted oldest first by completion time.
    """
    stamped = [(_parse_finished_at(r), r) for r in runs]
    dated = [(when, r) for when, r in stamped if when is not None]
    return [r for _, r in sorted(dated, key=lambda pair: pair[0])]


def _parse_finished_at(run: dict[str, Any]) -> dt.datetime | None:
    return _parse_instant(run.get("finished_at"))


def _compute_expected_interval_hours(
    scheduled: list[dict[str, Any]],
) -> float | None:
    """Calculates median interval between scheduled sweeps in hours.

    Uses median rather than mean to prevent isolated delays from redefining normal
    cadence.

    Args:
        scheduled: Chronologically sorted scheduled run dictionaries.

    Returns:
        Median interval in hours, or None if fewer than MIN_INTERVALS_FOR_OVERDUE runs exist.
    """
    if len(scheduled) < MIN_INTERVALS_FOR_OVERDUE:
        return None
    stamps = [_parse_finished_at(r) for r in scheduled]
    gaps = [
        (b - a).total_seconds() / 3600
        for a, b in itertools.pairwise(stamps)
        if a and b
    ]
    positive = [g for g in gaps if g > 0]
    return statistics.median(positive) if positive else None


def _format_last_run(run: dict[str, Any]) -> dict[str, Any]:
    """Formats the latest finished run for badge rendering.

    Returns None for session counts and pass rates when a run evaluated zero traces,
    avoiding an inaccurate 0% claim.

    Args:
        run: Terminal run dictionary.

    Returns:
        Dictionary of formatted run metrics and status.
    """
    counters = run.get("counters") or {}
    evaluated = int(counters.get("traces_evaluated") or 0)
    passed = int(counters.get("traces_eval_passed") or 0)
    return {
        "run_id": str(run.get("run_id") or ""),
        "finished_at": run.get("finished_at"),
        "status": str(run.get("status") or ""),
        "trigger_type": str(run.get("trigger_type") or ""),
        "sessions": evaluated or None,
        "sessions_passed": passed if evaluated else None,
        "pass_rate": (passed / evaluated) if evaluated else None,
    }


def _summarize_open_findings(
    insights: list[dict[str, Any]],
) -> tuple[int, dict[str, str] | None]:
    """Summarizes unresolved findings and identifies the highest-occurrence issue.

    Args:
        insights: List of insight dictionaries.

    Returns:
        Tuple of (unresolved count, worst finding summary dictionary or None).
    """
    open_ = [i for i in insights if str(i.get("status") or "") != "RESOLVED"]
    if not open_:
        return 0, None
    worst = max(open_, key=lambda i: int(i.get("trace_count") or 0))
    return len(open_), {
        "insight_id": str(worst.get("insight_id") or ""),
        "label": str(worst.get("label") or ""),
    }


def _parse_instant(value: Any) -> dt.datetime | None:
    """Parses an ISO timestamp string into a timezone-aware datetime.

    Tolerates trailing 'Z' notation and ensures UTC timezone is attached.

    Args:
        value: Timestamp string or arbitrary object.

    Returns:
        Parsed datetime instance, or None if parsing fails.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
