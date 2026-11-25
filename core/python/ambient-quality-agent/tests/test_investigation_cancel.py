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

"""Tests for cancelling an investigation.

A cancel only records `CANCELLED`. The running investigation stops itself:
every write it makes reads the stored status first
(`model.update_investigation`, `model.append_investigation_event`) and raises
`InvestigationCancelledError`. Its sweeps write a stage row before their first
page and after every page, so durable jobs and sync-mode threads stop the same
way, within a page.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import state as state_module
from ambient_quality_agent.core.investigation import job_execution as execute
from ambient_quality_agent.core.investigation import job_scheduling as schedule
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.nodes import _stage, review
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.investigations import health
from ambient_quality_agent.tools.investigations.models import (
    InvestigationStatus,
    StageProgress,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config

from .conftest import (
    StateContext,
    create_investigation,
    get_stored_investigation,
    make_agent_case,
    make_config,
    make_page,
)

_WINDOW = {
    "window_start": "2026-01-01T00:00:00+00:00",
    "window_end": "2026-01-08T00:00:00+00:00",
    "budget_per_metric": 50,
}


def _seed(status: InvestigationStatus, **fields: Any) -> tuple[dict, Any]:
    """Seed one investigation.

    Args:
        status: Status it is stored with.
        **fields: Other record fields.

    Returns:
        The state it is stored under and the created record.
    """
    state: dict[str, Any] = {}
    fields.setdefault("effective_config", dataclasses.asdict(make_config()))
    record = create_investigation(
        state, runs.Investigation(status=status, **fields)
    )
    return state, record


def _cancel(state: dict[str, Any], investigation_id: str) -> dict[str, Any]:
    return asyncio.run(
        runs.cancel_investigation(investigation_id, StateContext(state))
    )


# --- cancel_investigation: a status write ---------------------------------- #


@pytest.mark.parametrize(
    "status",
    [
        InvestigationStatus.SCHEDULED,
        InvestigationStatus.PENDING,
        InvestigationStatus.RUNNING,
    ],
)
def test_an_investigation_that_has_not_finished_is_cancelled(
    status: InvestigationStatus,
) -> None:
    state, record = _seed(status)

    result = _cancel(state, record.investigation_id)

    assert result["status"] == InvestigationStatus.CANCELLED
    stored = get_stored_investigation(state, record.investigation_id)
    assert stored.status == InvestigationStatus.CANCELLED
    assert stored.finished_at is not None
    assert [e.text for e in stored.events] == [runs.CANCELLED_EVENT_TEXT]


@pytest.mark.parametrize(
    "status",
    [
        InvestigationStatus.DONE,
        InvestigationStatus.FAILED,
        InvestigationStatus.SKIPPED,
        InvestigationStatus.CANCELLED,
    ],
)
def test_a_finished_investigation_cannot_be_cancelled(
    status: InvestigationStatus,
) -> None:
    state, record = _seed(status)

    result = _cancel(state, record.investigation_id)

    # An error envelope, with no status, so a caller cannot read it as the
    # record of a cancelled investigation.
    assert "already finished" in result["error"]
    assert "status" not in result
    assert get_stored_investigation(state, record.investigation_id).status == (
        status
    )


def test_an_unknown_investigation_cannot_be_cancelled() -> None:
    assert "No investigation found" in _cancel({}, "nope")["error"]


# --- the one check: every write an investigation makes --------------------- #


@pytest.mark.parametrize(
    "changes",
    [
        {"status": InvestigationStatus.RUNNING},
        {"status": InvestigationStatus.DONE, "summary": {"k": "v"}},
        {"status": InvestigationStatus.FAILED, "error": "late"},
        {"job_name": "projects/p/operations/1"},
    ],
)
def test_a_cancelled_investigation_takes_no_further_update(
    changes: dict,
) -> None:
    state, record = _seed(InvestigationStatus.CANCELLED)

    with pytest.raises(runs.InvestigationCancelledError):
        asyncio.run(
            runs.update_investigation(state, record.investigation_id, **changes)
        )

    stored = get_stored_investigation(state, record.investigation_id)
    assert stored.status == InvestigationStatus.CANCELLED
    assert stored.error is None


def test_a_cancelled_investigation_takes_no_further_event(
    investigation_store: Any,
) -> None:
    _, record = _seed(InvestigationStatus.CANCELLED)

    with pytest.raises(runs.InvestigationCancelledError):
        runs.append_investigation_event(
            investigation_store, record.investigation_id, "page 2"
        )

    assert investigation_store.event_rows == []


def test_a_running_investigation_takes_its_events(
    investigation_store: Any,
) -> None:
    _, record = _seed(InvestigationStatus.RUNNING)

    runs.append_investigation_event(
        investigation_store, record.investigation_id, "page 1", source="stage"
    )

    assert [r["text"] for r in investigation_store.event_rows] == ["page 1"]


def _write_events_with(monkeypatch: pytest.MonkeyPatch, error: Exception):
    """Make every event write raise `error`.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        error: What the write raises.
    """

    def failing(state: Any, text: str, source: str) -> None:
        raise error

    monkeypatch.setattr(state_module, "event_writer", failing)


def test_progress_and_stage_writes_let_the_cancel_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both are best-effort, except that a cancel must stop the investigation."""
    state = {"investigation_id": "r1"}
    _write_events_with(
        monkeypatch, runs.InvestigationCancelledError("r1 was cancelled")
    )
    with pytest.raises(runs.InvestigationCancelledError):
        WorkflowState.record_progress_event(state, "done", "review")
    with pytest.raises(runs.InvestigationCancelledError):
        WorkflowState.record_stage_event(state, StageProgress(stage="Review"))

    # A lost row is not worth failing the investigation over.
    _write_events_with(monkeypatch, RuntimeError("BigQuery unavailable"))
    WorkflowState.record_progress_event(state, "done", "review")
    WorkflowState.record_stage_event(state, StageProgress(stage="Review"))


def test_the_keep_alive_stops_quietly_once_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its re-write finds the investigation cancelled: the thread ends without
    an unhandled error, and the node's own next write stops the run."""
    monkeypatch.setattr(_stage, "_KEEP_ALIVE_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(_stage, "PROGRESS_INTERVAL_SECONDS", 0)
    reporter = _stage.StageReporter({"investigation_id": "r1"})
    reporter.write(StageProgress(stage="Review"))
    _write_events_with(
        monkeypatch, runs.InvestigationCancelledError("r1 was cancelled")
    )
    unhandled: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", unhandled.append)

    with reporter.keep_alive():
        threading.Event().wait(0.1)

    assert unhandled == []


# --- execution ------------------------------------------------------------- #


def test_a_job_cancelled_before_it_starts_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable job queued before the cancel stops at its first write."""
    graph = mock.AsyncMock()
    monkeypatch.setattr(execute, "run_investigation_graph", graph)
    logged = mock.MagicMock()
    monkeypatch.setattr(execute, "_log_investigation_finished", logged)
    state, record = _seed(InvestigationStatus.PENDING)
    _cancel(state, record.investigation_id)

    result = asyncio.run(
        execute.execute_investigation(
            record.investigation_id, StateContext(state)
        )
    )

    assert result == {"status": "cancelled"}
    graph.assert_not_awaited()
    assert get_stored_investigation(state, record.investigation_id).status == (
        InvestigationStatus.CANCELLED
    )
    assert logged.call_args.args[1] == {"status": InvestigationStatus.CANCELLED}


def test_a_graph_finishing_after_a_cancel_leaves_it_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landing after the last page is caught by the final write."""
    state, record = _seed(InvestigationStatus.PENDING)

    async def finish_after_the_cancel(*_: Any, **__: Any) -> dict[str, Any]:
        store = runs.store_factory(state)
        store.append(
            store.get(record.investigation_id, include_events=False).model_copy(
                update={"status": InvestigationStatus.CANCELLED}
            )
        )
        return {"status": "done", "summary": {}, "counters": {}}

    monkeypatch.setattr(
        execute, "run_investigation_graph", finish_after_the_cancel
    )
    logged = mock.MagicMock()
    monkeypatch.setattr(execute, "_log_investigation_finished", logged)

    result = asyncio.run(
        execute.execute_investigation(
            record.investigation_id, StateContext(state)
        )
    )

    assert result == {"status": "cancelled"}
    assert get_stored_investigation(state, record.investigation_id).status == (
        InvestigationStatus.CANCELLED
    )
    # The alert log reports the cancel, not the done the graph ended in.
    assert logged.call_args.args[1] == {"status": InvestigationStatus.CANCELLED}


def test_a_cancelled_investigation_is_not_worth_an_email() -> None:
    counters = {"insights_created": 3, "traces_eval_errored": 2}

    assert execute._should_notify(InvestigationStatus.CANCELLED, counters) == (
        False,
        "",
    )


def test_the_graph_stops_at_its_first_stage_row_once_cancelled(
    patched_factories: dict[str, Any],
) -> None:
    """The real graph: the stage row it writes before fetching anything finds
    it cancelled."""
    _, record = _seed(InvestigationStatus.CANCELLED)

    with pytest.raises(runs.InvestigationCancelledError):
        asyncio.run(
            execute.run_investigation_graph(
                dataclasses.asdict(make_config()),
                investigation_id=record.investigation_id,
                **_WINDOW,
            )
        )

    assert patched_factories["fetcher"].page_tokens_seen == []
    assert patched_factories["insight_store"].calls == []


# --- end to end ------------------------------------------------------------ #


def _review_then_cancel(
    patched_factories: dict[str, Any], state: dict[str, Any], store: Any
) -> None:
    """Make the reviewer cancel the investigation while page 1 is reviewed.

    Page 1 holds one session past the review window so `evaluate` collects at
    least one completed review before returning, and `finish_page` writes a
    stage row before page 2 is fetched.

    Args:
        patched_factories: The graph's fakes.
        state: State the investigation is stored under.
        store: The investigation store, to find the one being reviewed.
    """
    reviewer = patched_factories["reviewer"]
    cancel_lock = threading.Lock()
    cancelled = False

    def review_then_cancel(prompt: str) -> str:
        nonlocal cancelled
        with cancel_lock:
            if not cancelled:
                cancelled = True
                # The fake keeps BigQuery's rows, whose column is still `run_id`.
                investigation_id = store.rows[-1]["run_id"]
                asyncio.run(
                    runs.cancel_investigation(
                        investigation_id, StateContext(state)
                    )
                )
        return reviewer(prompt)

    patched_factories["reviewer"] = review_then_cancel
    first_page_size = 2 * review.REVIEW_CONCURRENCY + 1
    patched_factories["fetcher"]._pages = [
        make_page(
            [make_agent_case(f"s{n}") for n in range(first_page_size)],
            next_page_token="page-2",
        ),
        make_page([make_agent_case("s-next")]),
    ]


def test_a_sync_investigation_cancelled_mid_sweep_stops_at_the_next_page(
    monkeypatch: pytest.MonkeyPatch,
    patched_factories: dict[str, Any],
    investigation_store: Any,
) -> None:
    """The cancel arrives while page 1 is reviewed: page 2 is never fetched,
    and no insight is written."""
    cfg = make_config(sync_investigation=True)
    monkeypatch.setattr(effective_config, "load", lambda state: cfg)
    state: dict[str, Any] = {}
    _review_then_cancel(patched_factories, state, investigation_store)

    result = asyncio.run(schedule.schedule_investigation(StateContext(state)))

    assert result["status"] == InvestigationStatus.CANCELLED
    assert patched_factories["fetcher"].page_tokens_seen == [None]
    assert patched_factories["insight_store"].calls == []


def test_a_durable_investigation_cancelled_mid_sweep_stops_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    patched_factories: dict[str, Any],
    investigation_store: Any,
) -> None:
    """The same investigation as a durable job: submitted, then executed."""
    monkeypatch.setattr(effective_config, "load", lambda state: make_config())
    monkeypatch.setattr(
        schedule, "submit_investigation", lambda investigation_id, context: {}
    )
    state: dict[str, Any] = {}
    submitted = asyncio.run(
        schedule.schedule_investigation(StateContext(state))
    )
    _review_then_cancel(patched_factories, state, investigation_store)

    result = asyncio.run(
        execute.execute_investigation(
            submitted["investigation_id"], StateContext(state)
        )
    )

    assert result == {"status": "cancelled"}
    assert get_stored_investigation(
        state, submitted["investigation_id"]
    ).status == (InvestigationStatus.CANCELLED)
    assert patched_factories["fetcher"].page_tokens_seen == [None]
    assert patched_factories["insight_store"].calls == []


# --- health ---------------------------------------------------------------- #


def test_a_cancelled_sweep_is_neither_a_failure_nor_a_finish() -> None:
    failed = {
        "investigation_id": "a",
        "status": "failed",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "trigger_type": "scheduled",
    }
    cancelled = {
        "investigation_id": "b",
        "status": "cancelled",
        "finished_at": "2026-01-02T00:00:00+00:00",
        "trigger_type": "scheduled",
    }

    verdict = health.assess([failed, cancelled], [])

    assert verdict.verdict == health.FAILING
    assert verdict.last_investigation["investigation_id"] == "a"
    assert health.assess([cancelled], []).verdict == health.NEVER
