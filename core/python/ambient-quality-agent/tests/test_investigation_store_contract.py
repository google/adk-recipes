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

"""One suite, both implementations: what `InvestigationStore` promises.

Every test here runs twice, against the BigQuery store and against
`InMemoryInvestigationStore`, and asserts the same answer from both. That is the
point of the file. The two keep a run completely differently -- one as a history
of whole snapshots, because BigQuery has no cheap ``UPDATE``, the other as one
record it edits -- and the only thing holding them to one behavior is a suite
that cannot tell which it is talking to.

The BigQuery side is `FakeInvestigationStore`, which replaces the client and
nothing else, so the row shapes, the JSON encoding and the model validators are
all production code.

`test_investigation_store` covers the SQL itself, and `test_investigation_run`
the registry functions layered over this.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from ambient_quality_agent.standalone.investigations import (
    InMemoryInvestigationStore,
)
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.tools.investigations.models import (
    InvestigationCounters,
    InvestigationRecord,
    RunStatus,
)

from .conftest import FakeInvestigationStore

_AGENT = "watched-agent"


@pytest.fixture(params=["bigquery", "local"])
def store(request: pytest.FixtureRequest) -> Any:
    """Both implementations of the contract, one test body.

    Carries a ``backdate`` handle the lease tests need. Every write restamps
    ``updated_at`` and nothing in the contract sets it, so a run old enough to
    be stale cannot be made through the contract at all -- and how you reach
    past it is the one thing the two do differently. What is being tested
    either side of that handle is still one behavior.
    """
    if request.param == "bigquery":
        fake = FakeInvestigationStore()

        def backdate(run_id: str, *, minutes: int) -> None:
            for row in fake.rows:
                if row["run_id"] == run_id:
                    row["updated_at"] = _aged(minutes)

        fake.backdate = backdate  # type: ignore[attr-defined]
        return fake

    backing = InMemoryStore()
    in_memory = InMemoryInvestigationStore(backing)

    def backdate_saved_run(run_id: str, *, minutes: int) -> None:
        record = backing.runs[run_id]
        backing.save_run(
            record.model_copy(update={"updated_at": _aged(minutes)})
        )

    in_memory.backdate = backdate_saved_run  # type: ignore[attr-defined]
    return in_memory


def _aged(minutes: int) -> str:
    """Generates an ISO timestamp offset by the specified minutes into the past.

    Args:
        minutes: Offset duration in minutes.

    Returns:
        ISO timestamp string.
    """
    return (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes)
    ).isoformat()


def _run(run_id: str, **fields: Any) -> InvestigationRecord:
    """Builds an InvestigationRecord for testing with default fields.

    Args:
        run_id: Identifier for the investigation run.
        **fields: Field overrides for the record.

    Returns:
        Configured InvestigationRecord instance.
    """
    defaults: dict[str, Any] = {
        "observed_agent_name": _AGENT,
        "status": RunStatus.DONE,
        "trigger_type": "scheduled",
        "window_end": "2026-01-01T00:00:00+00:00",
    }
    return InvestigationRecord(run_id=run_id, **{**defaults, **fields})


def _counters(**fields: int) -> InvestigationCounters:
    return InvestigationCounters(**fields)


# --- writing a run --------------------------------------------------------- #


def test_a_stored_run_reads_back(store: Any) -> None:
    store.append(_run("r1", summary={"note": "hello"}))

    record = store.get("r1")

    assert record is not None
    assert record.run_id == "r1"
    assert record.summary == {"note": "hello"}


def test_an_unknown_run_is_none(store: Any) -> None:
    assert store.get("nothing-wrote-this") is None


def test_storing_a_run_twice_leaves_one_run_in_its_newer_state(
    store: Any,
) -> None:
    """The contract's one genuinely load-bearing write rule.

    BigQuery gets a second snapshot and picks it on read; the local store
    replaces the record. Either way the registry holds one run, in the state it
    was last given -- a caller must never see the superseded one, and must never
    see two.
    """
    store.append(_run("r1", status=RunStatus.PENDING))
    store.append(_run("r1", status=RunStatus.DONE, error="fell over"))

    assert store.get("r1").status == RunStatus.DONE
    assert store.get("r1").error == "fell over"
    assert [r.run_id for r in store.list_recent()] == ["r1"]


def test_appending_restamps_updated_at(store: Any) -> None:
    stored = store.append(_run("r1", updated_at="2020-01-01T00:00:00+00:00"))

    assert stored.updated_at != "2020-01-01T00:00:00+00:00"
    assert store.get("r1").updated_at == stored.updated_at


# --- events ---------------------------------------------------------------- #


def test_events_come_back_in_the_order_they_were_emitted(store: Any) -> None:
    store.append(_run("r1"))
    store.append_event("r1", "first", source="init")
    store.append_event("r1", "second", source="review")

    events = store.get("r1").events

    assert [e.text for e in events] == ["first", "second"]
    assert [e.source for e in events] == ["init", "review"]


def test_a_read_can_decline_the_events(store: Any) -> None:
    store.append(_run("r1"))
    store.append_event("r1", "noise")

    assert store.get("r1", include_events=False).events == []


def test_updating_a_run_does_not_drop_the_events_it_already_emitted(
    store: Any,
) -> None:
    """The reason events are not a field on the record.

    A sweep emits events while it runs and the record is rewritten at the end of
    it, so a write that carried the events would erase everything the nodes said.
    """
    store.append(_run("r1", status=RunStatus.RUNNING))
    store.append_event("r1", "the sweep said something")
    store.append(_run("r1", status=RunStatus.DONE))

    assert [e.text for e in store.get("r1").events] == [
        "the sweep said something"
    ]


def test_a_records_own_events_are_not_written_back(store: Any) -> None:
    """Only `append_event` creates an event.

    Asserted through `include_events=False`, the read that returns the record as
    stored rather than overlaying the events on it -- and the read `update_run`
    makes before rewriting a run. A write that kept the events it was handed
    would put them back into the record on every update.
    """
    store.append(_run("r1"))
    store.append_event("r1", "once")

    store.append(store.get("r1"))

    assert store.get("r1", include_events=False).events == []
    assert [e.text for e in store.get("r1").events] == ["once"]


# --- listing --------------------------------------------------------------- #


def test_the_list_is_oldest_first(store: Any) -> None:
    store.append(_run("old", created_at="2026-01-01T00:00:00+00:00"))
    store.append(_run("new", created_at="2026-01-03T00:00:00+00:00"))
    store.append(_run("mid", created_at="2026-01-02T00:00:00+00:00"))

    assert [r.run_id for r in store.list_recent()] == ["old", "mid", "new"]


def test_the_cap_keeps_the_most_recent_and_still_reads_oldest_first(
    store: Any,
) -> None:
    """Capped from the newest end, presented from the oldest. A cap taken off
    the front would hand a reader the deployment's first three runs forever."""
    for day in range(1, 6):
        store.append(
            _run(f"r{day}", created_at=f"2026-01-0{day}T00:00:00+00:00")
        )

    assert [r.run_id for r in store.list_recent(limit=3)] == ["r3", "r4", "r5"]


def test_the_list_filters_on_the_run_current_status(store: Any) -> None:
    store.append(_run("done"))
    store.append(_run("failed", status=RunStatus.FAILED))

    listed = store.list_recent(status=RunStatus.FAILED)

    assert [r.run_id for r in listed] == ["failed"]


def test_a_run_that_moved_on_is_not_matched_on_its_old_status(
    store: Any,
) -> None:
    """The status filter applies to what the run *is*, not to anything it was."""
    store.append(_run("r1", status=RunStatus.RUNNING))
    store.append(_run("r1", status=RunStatus.DONE))

    assert store.list_recent(status=RunStatus.RUNNING) == []
    assert [r.run_id for r in store.list_recent(status=RunStatus.DONE)] == [
        "r1"
    ]


def test_the_window_filters_on_what_a_sweep_covered_not_when_it_ran(
    store: Any,
) -> None:
    """Product behaviour, and the one filter rule worth stating twice: a sweep
    of Monday's traffic that executed on Tuesday counts against Monday."""
    store.append(
        _run(
            "monday",
            window_end="2026-03-02T23:00:00+00:00",
            created_at="2026-03-03T09:00:00+00:00",
        )
    )
    store.append(
        _run(
            "tuesday",
            window_end="2026-03-03T23:00:00+00:00",
            created_at="2026-03-04T09:00:00+00:00",
        )
    )

    listed = store.list_recent(
        window_start="2026-03-02T00:00:00+00:00",
        window_end="2026-03-03T00:00:00+00:00",
    )

    assert [r.run_id for r in listed] == ["monday"]


def test_a_run_with_no_window_falls_outside_every_period(store: Any) -> None:
    store.append(_run("windowless", window_end=None))

    assert store.list_recent(window_start="2026-01-01T00:00:00+00:00") == []
    assert [r.run_id for r in store.list_recent()] == ["windowless"]


# --- totals ---------------------------------------------------------------- #


def test_totals_sum_the_counters_and_report_the_span_actually_covered(
    store: Any,
) -> None:
    """A caller labelling its figures with the period it asked for rather than
    the one they cover claims a quiet fortnight where there was an empty one."""
    store.append(
        _run(
            "r1",
            window_end="2026-02-01T00:00:00+00:00",
            counters=_counters(insights_created=2, traces_scanned=10),
        )
    )
    store.append(
        _run(
            "r2",
            window_end="2026-02-05T00:00:00+00:00",
            counters=_counters(insights_created=3, traces_scanned=7),
        )
    )

    totals, covered = store.sum_counters()

    assert totals["investigations"] == 2
    assert totals["insights_created"] == 5
    assert totals["traces_scanned"] == 17
    assert covered == {
        "start": "2026-02-01T00:00:00+00:00",
        "end": "2026-02-05T00:00:00+00:00",
    }


def test_totals_carry_every_counter_even_at_zero(store: Any) -> None:
    """One shape off every registry, so a caller reads the same keys whatever
    produced them -- including a counter no stored run has ever carried."""
    store.append(_run("r1", counters=_counters(insights_created=1)))

    totals, _ = store.sum_counters()

    assert set(InvestigationCounters.model_fields) <= set(totals)
    assert totals["clusters_rejected"] == 0


def test_a_run_updated_several_times_is_counted_once(store: Any) -> None:
    store.append(_run("r1", counters=_counters(insights_created=4)))
    store.append(_run("r1", counters=_counters(insights_created=4)))

    totals, _ = store.sum_counters()

    assert totals["investigations"] == 1
    assert totals["insights_created"] == 4


# --- per day --------------------------------------------------------------- #


def test_days_are_bucketed_on_the_window_and_ordered_oldest_first(
    store: Any,
) -> None:
    store.append(
        _run(
            "a",
            window_end="2026-04-02T06:00:00+00:00",
            counters=_counters(insights_created=1),
        )
    )
    store.append(
        _run(
            "b",
            window_end="2026-04-02T18:00:00+00:00",
            counters=_counters(insights_created=2),
        )
    )
    store.append(
        _run(
            "c",
            window_end="2026-04-04T06:00:00+00:00",
            counters=_counters(insights_created=5),
        )
    )

    days = store.sum_counters_by_day()

    assert [d["day"] for d in days] == ["2026-04-02", "2026-04-04"]
    assert days[0]["investigations"] == 2
    assert days[0]["insights_created"] == 3
    assert days[1]["insights_created"] == 5


def test_a_day_nothing_ran_is_absent_rather_than_zero(store: Any) -> None:
    """A zero row cannot be told from a day the deployment was down, and only
    the caller knows which days its chart means to show."""
    store.append(_run("a", window_end="2026-04-02T06:00:00+00:00"))
    store.append(_run("c", window_end="2026-04-04T06:00:00+00:00"))

    assert [d["day"] for d in store.sum_counters_by_day()] == [
        "2026-04-02",
        "2026-04-04",
    ]


def test_a_run_that_measured_nothing_is_counted_as_unmeasured(
    store: Any,
) -> None:
    """ "Swept and found nothing" and "never swept" are different days, and
    adding the zeros in would erase the difference."""
    store.append(
        _run(
            "measured",
            window_end="2026-04-02T06:00:00+00:00",
            counters=_counters(traces_scanned=3),
        )
    )
    store.append(_run("empty", window_end="2026-04-02T07:00:00+00:00"))

    day = store.sum_counters_by_day()[0]

    assert day["investigations"] == 2
    assert day["unmeasured"] == 1


def test_a_run_with_no_window_is_filed_under_no_day(store: Any) -> None:
    store.append(
        _run(
            "windowless", window_end=None, counters=_counters(traces_scanned=9)
        )
    )

    assert store.sum_counters_by_day() == []


# --- the ambient watermark ------------------------------------------------- #


def test_the_watermark_is_the_newest_finished_ambient_window(
    store: Any,
) -> None:
    store.append(_run("older", window_end="2026-05-01T00:00:00+00:00"))
    store.append(_run("newer", window_end="2026-05-04T00:00:00+00:00"))

    assert (
        store.get_last_finished_window_end(_AGENT)
        == "2026-05-04T00:00:00+00:00"
    )


@pytest.mark.parametrize(
    ("label", "fields"),
    [
        ("unfinished", {"status": RunStatus.RUNNING}),
        ("failed", {"status": RunStatus.FAILED}),
        ("manual", {"trigger_type": "manual"}),
        ("a dry run", {"dry_run": True}),
        ("another agent", {"observed_agent_name": "somebody-else"}),
    ],
)
def test_the_watermark_ignores_runs_that_must_not_move_it(
    store: Any, label: str, fields: dict[str, Any]
) -> None:
    """Each of these would advance the next ambient window past telemetry no
    sweep has actually looked at."""
    store.append(_run("kept", window_end="2026-05-01T00:00:00+00:00"))
    store.append(
        _run("skipped", window_end="2026-05-09T00:00:00+00:00", **fields)
    )

    assert (
        store.get_last_finished_window_end(_AGENT)
        == "2026-05-01T00:00:00+00:00"
    ), label


def test_the_watermark_is_none_before_any_run_finishes(store: Any) -> None:
    assert store.get_last_finished_window_end(_AGENT) is None


# --- runs nothing came back for -------------------------------------------- #


def test_a_pending_run_past_its_lease_is_found(store: Any) -> None:
    """Nothing revisits a run once its submission returned, so a job that died
    before writing `running` leaves a row that never settles."""
    store.append(_run("abandoned", status=RunStatus.PENDING))
    store.backdate("abandoned", minutes=120)

    assert store.list_stale_pending(lease_minutes=30) == ["abandoned"]


def test_a_pending_run_inside_its_lease_is_left_alone(store: Any) -> None:
    """A submitted job can sit queued a while before it writes anything."""
    store.append(_run("fresh", status=RunStatus.PENDING))
    store.backdate("fresh", minutes=5)

    assert store.list_stale_pending(lease_minutes=30) == []


@pytest.mark.parametrize(
    "status",
    [RunStatus.RUNNING, RunStatus.DONE, RunStatus.FAILED, RunStatus.SKIPPED],
)
def test_only_pending_runs_are_stale(store: Any, status: RunStatus) -> None:
    """Anything past `pending` has reported something about itself. A long
    `running` sweep is slow, not lost, and failing it would end a live run."""
    store.append(_run("moved-on", status=status))
    store.backdate("moved-on", minutes=600)

    assert store.list_stale_pending(lease_minutes=30) == []


def test_a_run_that_left_pending_is_no_longer_stale(store: Any) -> None:
    """The age is read off the run's current state, not off anything it was."""
    store.append(_run("r1", status=RunStatus.PENDING))
    store.append(_run("r1", status=RunStatus.DONE))
    store.backdate("r1", minutes=600)

    assert store.list_stale_pending(lease_minutes=30) == []


def test_a_scheduled_run_past_its_grace_is_overdue(store: Any) -> None:
    """A deleted Cloud Task never fires, and nothing else moves the run on."""
    store.append(_run("lost", status=RunStatus.SCHEDULED, due_at=_aged(120)))

    assert store.list_overdue_scheduled(grace_minutes=60) == ["lost"]


@pytest.mark.parametrize("minutes_ago", [-15, 30])
def test_a_scheduled_run_inside_its_grace_is_left_alone(
    store: Any, minutes_ago: int
) -> None:
    """Not yet due, or due a little while ago with a retry still to come."""
    store.append(
        _run("waiting", status=RunStatus.SCHEDULED, due_at=_aged(minutes_ago))
    )

    assert store.list_overdue_scheduled(grace_minutes=60) == []


def test_a_scheduled_run_that_started_is_not_overdue(store: Any) -> None:
    """Read off the run's current state: its trigger fired and it moved on."""
    store.append(_run("r1", status=RunStatus.SCHEDULED, due_at=_aged(120)))
    store.append(_run("r1", status=RunStatus.PENDING, due_at=_aged(120)))

    assert store.list_overdue_scheduled(grace_minutes=60) == []


def test_a_scheduled_run_is_not_in_flight(store: Any) -> None:
    """Nothing runs for it until its trigger fires, so it must not hold back a
    custom investigation."""
    store.append(_run("waiting", status=RunStatus.SCHEDULED, due_at=_aged(-15)))
    store.append(_run("queued", status=RunStatus.PENDING))
    store.append(_run("going", status=RunStatus.RUNNING))
    store.append(_run("over", status=RunStatus.DONE))

    assert store.count_in_flight(_AGENT) == 2


# --- what only the in-memory store has to promise --------------------------- #


def test_a_reader_never_sees_a_half_applied_write() -> None:
    """The reason mutations rebind rather than mutate in place.

    Readers take no lock, so the only thing making them safe is that a write is
    one atomic rebinding. A reader holding the mapping keeps reading the state
    it started from, whatever a writer does next.
    """
    backing = InMemoryStore()
    store = InMemoryInvestigationStore(backing)
    store.append(_run("r1"))

    before = backing.runs
    store.append(_run("r2"))

    assert set(before) == {"r1"}, "the mapping a reader held changed under it"
    assert set(backing.runs) == {"r1", "r2"}


def test_concurrent_writers_lose_nothing() -> None:
    """One sweep writes at a time today, but the lock is what makes that a
    property of the store rather than of the caller."""
    backing = InMemoryStore()
    store = InMemoryInvestigationStore(backing)
    start = threading.Barrier(8)

    def write(n: int) -> None:
        start.wait(timeout=5)
        store.append(_run(f"r{n}"))
        store.append_event("shared", f"event {n}")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(backing.runs) == 8
    assert len(backing.list_run_events("shared")) == 8


def test_a_day_is_the_utc_day_whatever_offset_the_window_carries() -> None:
    """BigQuery buckets on ``DATE(window_end)``, which is the UTC date. Slicing
    the first ten characters off the stored string would file a late-evening
    sweep in a positive offset under tomorrow."""
    store = InMemoryInvestigationStore(InMemoryStore())
    store.append(_run("r1", window_end="2026-04-03T01:30:00+05:00"))

    assert [d["day"] for d in store.sum_counters_by_day()] == ["2026-04-02"]


def test_an_unparseable_window_is_dropped_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record whose window is not an instant cannot be filed or filtered, and
    must not take the dashboard down with it."""
    store = InMemoryInvestigationStore(InMemoryStore())
    store.append(_run("bad", window_end="not a timestamp"))

    with caplog.at_level("WARNING"):
        assert store.sum_counters_by_day() == []
        assert store.list_recent(window_start="2026-01-01T00:00:00+00:00") == []

    assert any("unparseable window bound" in r.message for r in caplog.records)


def test_a_naive_window_bound_is_read_as_utc() -> None:
    """Everything AQuA writes is offset-aware, but comparing aware with naive
    raises -- so a hand-built record must not crash a filtered read."""
    store = InMemoryInvestigationStore(InMemoryStore())
    store.append(_run("naive", window_end="2026-06-01T12:00:00"))

    listed = store.list_recent(
        window_start="2026-06-01T00:00:00+00:00",
        window_end="2026-06-02T00:00:00+00:00",
    )

    assert [r.run_id for r in listed] == ["naive"]


def test_the_store_starts_empty() -> None:
    """A standalone session begins with no history and ends with the process,
    which is what the bug asks for."""
    store = InMemoryStore()

    assert store.runs == {}
    assert store.list_run_events("anything") == ()
