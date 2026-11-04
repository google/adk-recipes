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

1. ``failing`` -- the most recent finished investigation ended in `FAILED`. The loop is
   turning and producing errors, which needs attention now whether or not the
   schedule is being kept.
2. ``never`` -- nothing has ever finished. Either the scheduler has not fired
   or every investigation so far is still in flight; the reason says which. A standalone
   run has no scheduler, so there the reason says to start a sweep instead.
3. ``overdue`` -- the newest finished investigation is older than `OVERDUE_FACTOR` times
   the interval this deployment actually runs at, and nothing is in flight.
4. ``watching`` -- none of the above.

**Where the expected interval comes from.** The median gap between recent
scheduled investigations, so an isolated outage does not skew the cadence. The
cron expression passed as `schedule` is displayed alongside the verdict without
affecting the threshold.

With fewer than `MIN_INTERVALS_FOR_OVERDUE` scheduled investigations, `overdue`
is unreachable and `max_age_hours` is null, avoiding false alarms on newly
attached agents.

**Anything in flight counts as active.** A pending or running investigation
suppresses `overdue` regardless of when the previous one finished.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import statistics
from typing import Any

from ambient_quality_agent.tools.investigations.models import (
    NON_AMBIENT_TRIGGER_TYPES,
    InvestigationStatus,
)

WATCHING = "watching"
OVERDUE = "overdue"
NEVER = "never"
FAILING = "failing"

OVERDUE_FACTOR = 2.0
"""Multiplier on the median interval before the ambient loop is marked `overdue`.

Allows for execution duration and scheduler jitter so normal daily schedules do
not flap into `overdue`.
"""

MIN_INTERVALS_FOR_OVERDUE = 3
"""Minimum completed scheduled investigations required before judging `overdue`
(three investigations yield two intervals for a median)."""

IN_FLIGHT = frozenset(
    {InvestigationStatus.PENDING, InvestigationStatus.RUNNING}
)


@dataclasses.dataclass(frozen=True)
class Health:
    """The verdict and everything the badge renders beside it."""

    verdict: str
    reason: str
    schedule: str | None
    max_age_hours: float | None
    last_scheduled_finish: str | None
    last_investigation: dict[str, Any] | None
    open_findings: int
    worst_finding: dict[str, str] | None

    def to_payload(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def assess(
    investigations: list[dict[str, Any]],
    insights: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
    schedule: str | None = None,
    standalone: bool = False,
) -> Health:
    """Computes deployment health verdict from investigations and open insights.

    Evaluates finished investigations, active sweeps, and open findings against expected intervals.

    Args:
        investigations: List of investigation dictionaries.
        insights: List of insight dictionaries.
        now: Optional reference timestamp (defaults to UTC now).
        schedule: Optional cron schedule expression string.
        standalone: Whether this is a standalone run, which has no scheduler
            and sweeps only when someone starts one.

    Returns:
        Health instance containing verdict, reason, and summary metrics.
    """
    now = now or dt.datetime.now(tz=dt.UTC)
    finished = _list_finished_investigations(investigations)
    scheduled = [
        r
        for r in finished
        if r.get("trigger_type") not in NON_AMBIENT_TRIGGER_TYPES
    ]
    in_flight = [
        r for r in investigations if str(r.get("status") or "") in IN_FLIGHT
    ]

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
        "last_investigation": _format_last_investigation(finished[-1])
        if finished
        else None,
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

    if str(finished[-1].get("status") or "") == InvestigationStatus.FAILED:
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


def _list_finished_investigations(
    investigations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filters and orders investigations that reached a terminal state.

    Orders investigations by parsed timestamps to ensure consistent sorting
    across timezone representations.

    Args:
        investigations: List of investigation dictionaries.

    Returns:
        Terminal investigation dictionaries sorted oldest first by completion
        time.
    """
    stamped = [(_parse_finished_at(r), r) for r in investigations]
    dated = [(when, r) for when, r in stamped if when is not None]
    return [r for _, r in sorted(dated, key=lambda pair: pair[0])]


def _parse_finished_at(investigation: dict[str, Any]) -> dt.datetime | None:
    return _parse_instant(investigation.get("finished_at"))


def _compute_expected_interval_hours(
    scheduled: list[dict[str, Any]],
) -> float | None:
    """Calculates median interval between scheduled investigations in hours.

    Uses median rather than mean to prevent isolated delays from redefining normal
    cadence.

    Args:
        scheduled: Chronologically sorted scheduled investigation dictionaries.

    Returns:
        Median interval in hours, or None if fewer than
        `MIN_INTERVALS_FOR_OVERDUE` investigations exist.
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


def _format_last_investigation(
    investigation: dict[str, Any],
) -> dict[str, Any]:
    """Formats the latest finished investigation for badge rendering.

    Returns None for trajectory counts and pass rates when an investigation
    evaluated zero trajectories, avoiding an inaccurate 0% claim.

    Args:
        investigation: Terminal investigation dictionary.

    Returns:
        Dictionary of formatted investigation metrics and status.
    """
    counters = investigation.get("counters") or {}
    evaluated = int(counters.get("trajectories_evaluated") or 0)
    passed = int(counters.get("trajectories_eval_passed") or 0)
    return {
        "investigation_id": str(investigation.get("investigation_id") or ""),
        "finished_at": investigation.get("finished_at"),
        "status": str(investigation.get("status") or ""),
        "trigger_type": str(investigation.get("trigger_type") or ""),
        "trajectories": evaluated or None,
        "trajectories_passed": passed if evaluated else None,
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
    worst = max(open_, key=lambda i: int(i.get("trajectory_count") or 0))
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
