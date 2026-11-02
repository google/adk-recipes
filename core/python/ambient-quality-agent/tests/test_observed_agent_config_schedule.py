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

"""When an attached agent's scheduled investigation is due on a scheduler tick."""

from __future__ import annotations

import datetime as dt
import pathlib
import re
import time

import pytest
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.tools.observed_agent_config import schedule, store

_NOON = dt.datetime(2026, 10, 13, 12, 0, tzinfo=dt.UTC)


def _list_ticks(day: dt.datetime) -> list[dt.datetime]:
    """Lists every tick of one UTC day.

    Args:
        day: Midnight of the day.

    Returns:
        The ticks, in order.
    """
    count = dt.timedelta(days=1) // schedule.TICK_PERIOD
    return [day + i * schedule.TICK_PERIOD for i in range(count)]


def test_the_tick_period_is_the_one_terraform_schedules() -> None:
    """The engine's due check assumes ticks this far apart."""
    ingress = (
        pathlib.Path(__file__).resolve().parents[1]
        / "terraform/modules/aqa/ingress.tf"
    ).read_text()
    job = re.search(
        r'resource "google_cloud_scheduler_job" "ambient_tick" \{(.*?)\n\}',
        ingress,
        re.S,
    )
    assert job is not None
    cron = re.search(r'\bschedule\s*=\s*"([^"]*)"', job.group(1))
    assert cron is not None
    minutes = int(schedule.TICK_PERIOD.total_seconds() // 60)
    assert cron.group(1) == f"*/{minutes} * * * *"


@pytest.mark.parametrize(
    "expression",
    [
        "0 12 * * *",
        "*/30 * * * *",
        "0 6 * * MON-FRI",
        "0 9 1 JAN,JUL *",
        "1-5/2 0-23/3 * * 1,3,5",
        "0 12 * * SUN-SAT/2",
        store.DEFAULT_INVESTIGATION_SCHEDULE,
    ],
)
def test_a_five_field_cron_is_valid(expression: str) -> None:
    assert schedule.is_valid_schedule(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "noon",
        "0 12 * *",
        # Seconds and years are croniter extensions, not unix-cron.
        "0 0 12 * * *",
        "@daily",
        # Random values would be drawn again on every tick.
        "R 12 * * *",
        "0 R * * *",
        "H 12 * * *",
        # croniter extensions beyond unix-cron.
        "0 12 L * *",
        "0 12 15W * *",
        "0 12 * * 5#2",
        "0 12 ? * *",
        # Never exists, so would never run.
        "0 0 31 2 *",
        "61 12 * * *",
    ],
)
def test_a_cron_the_engine_cannot_run_is_not_valid(expression: str) -> None:
    assert not schedule.is_valid_schedule(expression)


def test_a_schedule_is_due_in_the_tick_its_instant_ends() -> None:
    """The period is (tick - TICK_PERIOD, tick]: an instant on a tick belongs
    to that tick, not to the next."""
    assert schedule.is_schedule_due("0 12 * * *", _NOON)
    assert not schedule.is_schedule_due(
        "0 12 * * *", _NOON + schedule.TICK_PERIOD
    )
    assert not schedule.is_schedule_due(
        "0 12 * * *", _NOON - schedule.TICK_PERIOD
    )


def test_an_instant_between_ticks_is_due_in_the_tick_after_it() -> None:
    assert schedule.is_schedule_due("10 12 * * *", _NOON + schedule.TICK_PERIOD)
    assert not schedule.is_schedule_due("10 12 * * *", _NOON)


@pytest.mark.parametrize(
    ("expression", "due_ticks"),
    [
        ("0 12 * * *", 1),
        ("7 3,15 * * *", 2),
        # More often than the tick: one investigation per tick.
        ("*/5 * * * *", 96),
    ],
)
def test_consecutive_ticks_cover_each_instant_once(
    expression: str, due_ticks: int
) -> None:
    midnight = _NOON.replace(hour=0)

    due = [
        tick
        for tick in _list_ticks(midnight)
        if schedule.is_schedule_due(expression, tick)
    ]

    assert len(due) == due_ticks


def test_a_tick_in_another_zone_is_read_in_utc() -> None:
    """Cloud Scheduler sends the instant in the job's own zone; the cron is
    UTC whatever that zone is."""
    pacific = dt.timezone(dt.timedelta(hours=-7))

    assert schedule.is_schedule_due("0 12 * * *", _NOON.astimezone(pacific))


def test_an_instant_with_no_zone_is_read_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not as the host's zone, which would move the grid off UTC."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        naive = _NOON.replace(tzinfo=None)

        assert schedule.compute_tick(naive + dt.timedelta(minutes=5)) == _NOON
        assert schedule.is_schedule_due("0 12 * * *", naive)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    ("instant", "tick"),
    [
        (_NOON, _NOON),
        (_NOON + dt.timedelta(minutes=10), _NOON),
        (_NOON + dt.timedelta(minutes=14, seconds=59), _NOON),
        (_NOON + dt.timedelta(minutes=15), _NOON + schedule.TICK_PERIOD),
        # The same instant in another zone is on the same tick.
        (
            (_NOON + dt.timedelta(minutes=20)).astimezone(
                dt.timezone(dt.timedelta(hours=5, minutes=30))
            ),
            _NOON + schedule.TICK_PERIOD,
        ),
    ],
)
def test_an_instant_belongs_to_the_tick_at_or_before_it(
    instant: dt.datetime, tick: dt.datetime
) -> None:
    assert schedule.compute_tick(instant) == tick


def test_an_unrunnable_schedule_raises() -> None:
    with pytest.raises(ValueError, match="five-field"):
        schedule.is_schedule_due("@daily", _NOON)


def _build_record(**fields: object) -> store.AgentRecord:
    """Builds an attachment.

    Args:
        **fields: `AgentRecord` fields.

    Returns:
        The record.
    """
    return store.AgentRecord(
        config=ObservedAgentConfig(observed_agent_name="agent"), **fields
    )


def test_the_attachments_schedule_is_the_one_run() -> None:
    record = _build_record(investigation_schedule="0 6 * * *")

    assert schedule.resolve_investigation_schedule(record) == "0 6 * * *"


def test_a_paused_attachment_runs_no_schedule() -> None:
    record = _build_record(scheduled_trigger_enabled=False)

    assert schedule.resolve_investigation_schedule(record) is None


def test_an_agent_with_no_attachment_runs_on_the_default() -> None:
    assert (
        schedule.resolve_investigation_schedule(None)
        == store.DEFAULT_INVESTIGATION_SCHEDULE
    )
