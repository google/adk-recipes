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

"""When an attached agent's scheduled investigation is due.

The schedule is a field of the attachment (`store.AgentRecord`), not a Cloud
Scheduler job. One Cloud Scheduler job per AQuA instance calls the engine's
trigger route every `TICK_PERIOD`, and each tick asks `is_schedule_due`, for
every attached agent, whether its cron fell due since the tick before it.
Changing or pausing a schedule is therefore a write to the attachment, and
takes effect on the next tick.

The cron is read in UTC. A scheduled investigation starts up to one
`TICK_PERIOD` after the time its cron names, and a cron that fires more often
than the tick starts at most one investigation per tick. Neither loses
telemetry: each investigation checks everything since the last one that
finished.
"""

from __future__ import annotations

import datetime as dt
import re

from ambient_quality_agent.tools.observed_agent_config import store
from croniter import croniter

TICK_PERIOD = dt.timedelta(minutes=15)
"""How often Cloud Scheduler calls the trigger route.

The same period as the `ambient_tick` job in `terraform/modules/aqa`, which a
test pins."""

_FIELD_COUNT = 5
"""Minute, hour, day of month, month and day of week: the unix-cron fields."""

_FIELD_PATTERN = re.compile(
    r"(\*|\d+|[A-Za-z]{3})(-(\d+|[A-Za-z]{3}))?(/\d+)?"
    r"(,(\*|\d+|[A-Za-z]{3})(-(\d+|[A-Za-z]{3}))?(/\d+)?)*"
)
"""One unix-cron field: values, ranges and steps, in a list.

A value is `*`, a number, or a three-letter month or day name."""


def is_valid_schedule(expression: str) -> bool:
    """Checks that a schedule is a unix-cron expression that can fire.

    croniter accepts more than unix-cron: seconds and years as extra fields,
    `@daily`-style keywords, `R` for a random value drawn afresh on every
    tick, `H` for a hashed one, and `L`, `W`, `#` and `?`. Only unix-cron is
    accepted, so that a schedule reads as it would in a crontab. A date that never exists,
    such as 31 February, is refused too, since its schedule would never run.

    Args:
        expression: The cron expression, as stored.

    Returns:
        True if the expression is five unix-cron fields that name at least one
        real date.
    """
    fields = expression.split()
    if len(fields) != _FIELD_COUNT:
        return False
    if not all(_FIELD_PATTERN.fullmatch(field) for field in fields):
        return False
    return croniter.is_valid(expression, strict=True)


def compute_tick(instant: dt.datetime) -> dt.datetime:
    """Computes the tick an instant belongs to: the grid point at or before it.

    The grid starts at midnight UTC and steps by `TICK_PERIOD`, as the
    instance's Cloud Scheduler job does. A call off the grid, such as one with
    no schedule time, is checked and keyed as the tick before it, so it cannot
    start a second investigation for an instant a tick already covers.

    Args:
        instant: When the call was scheduled for, or received; UTC if it
            names no zone.

    Returns:
        The tick, in UTC.
    """
    instant = _convert_to_utc(instant)
    midnight = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + ((instant - midnight) // TICK_PERIOD) * TICK_PERIOD


def is_schedule_due(expression: str, tick: dt.datetime) -> bool:
    """Checks whether a schedule fell due in the tick period ending at `tick`.

    The period is `(tick - TICK_PERIOD, tick]`, so ticks that follow one
    another cover every instant once. Retries of one tick carry the same
    instant and reach the same answer.

    Args:
        expression: The cron expression, as stored.
        tick: The instant the tick was scheduled for; UTC if it names no zone.

    Returns:
        True if the cron names an instant in the period.

    Raises:
        ValueError: If `is_valid_schedule` refuses the expression.
    """
    if not is_valid_schedule(expression):
        raise ValueError(
            f"{expression!r} is not a five-field cron that names a real date"
        )
    tick = _convert_to_utc(tick)
    start = tick - TICK_PERIOD
    # `get_next` returns the first instant strictly after `start`.
    following = croniter(expression, start).get_next(dt.datetime)
    return following <= tick


def _convert_to_utc(instant: dt.datetime) -> dt.datetime:
    """Converts an instant to UTC, reading one that names no zone as UTC.

    `astimezone` would read a naive instant in the host's zone, which would
    move the grid on a host not set to UTC. `routes._parse_schedule_time`
    reads a naive schedule time the same way.

    Args:
        instant: The instant.

    Returns:
        The same instant in UTC.
    """
    if instant.tzinfo is None:
        return instant.replace(tzinfo=dt.UTC)
    return instant.astimezone(dt.UTC)


def resolve_investigation_schedule(
    record: store.AgentRecord | None,
) -> str | None:
    """Resolves the schedule the engine runs for an agent.

    Args:
        record: The agent's stored attachment; None for an agent with no
            attachment.

    Returns:
        The attachment's cron, or `DEFAULT_INVESTIGATION_SCHEDULE` when there
        is no attachment, or None when the attachment pauses its schedule.
    """
    if record is None:
        return store.DEFAULT_INVESTIGATION_SCHEDULE
    if not record.scheduled_trigger_enabled:
        return None
    return record.investigation_schedule
