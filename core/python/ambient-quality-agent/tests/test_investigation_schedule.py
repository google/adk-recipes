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

"""Tests for scheduling: derive, schedule tool, and self-query submit."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import threading
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.config import Config, ObservedAgentConfig
from ambient_quality_agent.core.investigation import job_scheduling as schedule
from ambient_quality_agent.core.investigation import locking
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.session_state import RouteContext
from ambient_quality_agent.tools.investigations.models import CustomOverrides
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    store,
)
from google.adk.sessions.state import State

from .conftest import (
    CallbackContext,
    StateContext,
    ToolContext,
    create_run,
    get_run,
    make_config,
)

# --- derive_window_and_budget --------------------------------------------- #


def _eval_config(**overrides: Any) -> Config:
    """Configures test options pinned to evaluation service mode.

    Args:
        **overrides: Configuration overrides.

    Returns:
        Config instance with eval_service quality mode.
    """
    return make_config(quality_analysis_mode="eval_service", **overrides)


def test_derive_window_and_budget_splits_cap() -> None:
    start, end, budget = schedule.derive_window_and_budget(_eval_config())
    # cap 100 across 2 metrics -> 50 each.
    assert budget == 50
    span = dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)
    assert span == dt.timedelta(days=7)


def test_derive_window_and_budget_zero_without_metrics() -> None:
    _, _, budget = schedule.derive_window_and_budget(
        _eval_config(multi_turn_metrics=[], single_turn_metrics=[])
    )
    assert budget == 0


def test_derive_window_and_budget_gives_review_mode_the_whole_cap() -> None:
    """Verify session_review mode allocates the entire cap without metric division."""
    _, _, budget = schedule.derive_window_and_budget(
        make_config(
            quality_analysis_mode="session_review",
            multi_turn_metrics=[],
            single_turn_metrics=[],
        )
    )
    assert budget == 100


def test_derive_window_and_budget_splits_across_every_selected_metric() -> None:
    # The cap is divided by however many metrics are selected across both scopes:
    # base is 2 metrics (cap 100 -> 50 each); selecting 4 -> 25 each.
    _, _, base_budget = schedule.derive_window_and_budget(_eval_config())
    _, _, wider_budget = schedule.derive_window_and_budget(
        _eval_config(
            multi_turn_metrics=[
                "task_success",
                "tool_use_quality",
                "trajectory_quality",
            ],
        )
    )
    assert base_budget == 50
    assert wider_budget == 25
    assert wider_budget < base_budget


def test_derive_window_and_budget_starts_at_the_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 1, 1, 6, tzinfo=dt.UTC),
    )
    since = "2026-01-01T00:30:00+00:00"
    start, end, budget = schedule.derive_window_and_budget(
        _eval_config(), since=since
    )
    assert start == since
    assert dt.datetime.fromisoformat(end) > dt.datetime.fromisoformat(start)
    assert budget == 50  # cap 100 / 2 metrics, unaffected by the window.


def test_derive_window_and_budget_without_a_mark_keeps_the_day_window() -> None:
    for since in (None, ""):
        start, end, _ = schedule.derive_window_and_budget(
            make_config(), since=since
        )
        span = dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)
        assert span == dt.timedelta(days=7)


def test_consecutive_windows_abut_when_the_mark_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = dt.datetime(2026, 1, 1, 0, 30, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(schedule, "_read_utc_clock", lambda: first)
    start1, end1, _ = schedule.derive_window_and_budget(
        make_config(), since="2026-01-01T00:00:00+00:00"
    )

    monkeypatch.setattr(
        schedule, "_read_utc_clock", lambda: first + dt.timedelta(hours=7)
    )
    start2, end2, _ = schedule.derive_window_and_budget(
        make_config(), since=end1
    )
    assert start2 == end1
    assert end2 == (first + dt.timedelta(hours=7)).isoformat()
    assert start1 != start2


def test_a_window_that_did_not_finish_is_covered_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 1, 1, 6, tzinfo=dt.UTC),
    )
    failed_from = "2026-01-01T00:00:00+00:00"
    start, _, _ = schedule.derive_window_and_budget(
        make_config(), since=failed_from
    )
    assert start == failed_from


# --- schedule_investigation tool (submit mocked) -------------------------- #


@pytest.fixture
def mock_submit(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Patch `submit_investigation` to return a job name (no real graph)."""
    fake = mock.MagicMock(return_value={"job_name": "op/123"})
    monkeypatch.setattr(schedule, "submit_investigation", fake)
    return fake


def test_a_run_is_scheduled_with_the_attached_agents_settings(
    mock_submit: mock.MagicMock,
    investigation_store: Any,
    jobs_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduling turn is where a run reads the configuration, and what it
    reads it snapshots -- so re-attaching mid-run cannot change a run already in
    flight."""
    monkeypatch.setattr(
        effective_config,
        "env_config",
        make_config(observed_agent_name="watched-agent"),
    )
    jobs_store.write_text(
        store.build_object_name("watched-agent"),
        json.dumps(
            {"schema_version": 1, "agent": {"data_lookback_window": 14}}
        ),
    )

    tc = StateContext()
    run_id = asyncio.run(schedule.schedule_investigation(tc))["run_id"]

    record = get_run(tc.state, run_id)
    assert record.effective_config["data_lookback_window"] == 14
    span = dt.datetime.fromisoformat(
        record.window_end
    ) - dt.datetime.fromisoformat(record.window_start)
    assert span == dt.timedelta(days=14)


def test_a_deployment_with_no_agent_starts_no_run(
    mock_submit: mock.MagicMock,
    investigation_store: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        effective_config, "env_config", make_config(observed_agent_name="")
    )

    result = asyncio.run(schedule.schedule_investigation(StateContext()))

    assert result == {"error": schedule.NO_AGENT_ATTACHED}
    mock_submit.assert_not_called()


def test_schedule_tool_returns_pending(mock_submit: mock.MagicMock) -> None:
    tc = StateContext()
    result = asyncio.run(schedule.schedule_investigation(tc))

    # Non-blocking: returns a pending run immediately.
    assert result["status"] == runs.RunStatus.PENDING
    # A durable job was submitted for this run.
    mock_submit.assert_called_once()
    assert mock_submit.call_args.args[0] == result["run_id"]
    # The config snapshot is stashed for the durable invocation to use.
    record = get_run(tc.state, result["run_id"])
    assert record.effective_config
    assert record.job_name == "op/123"


def test_schedule_tool_records_submit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = mock.MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(schedule, "submit_investigation", fake)

    result = asyncio.run(schedule.schedule_investigation(StateContext()))
    assert result["status"] == runs.RunStatus.FAILED
    assert "boom" in result["error"]


def test_submitting_a_run_fails_the_ones_left_pending(
    mock_submit: mock.MagicMock,
    investigation_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the sweep of stale runs happens. A deployment submits runs
    regularly, so that moment needs no job of its own to deploy and watch."""
    _lease(monkeypatch, minutes=30)
    tc = StateContext()
    wedged = asyncio.run(schedule.schedule_investigation(tc))["run_id"]
    _age_run(investigation_store, wedged, hours=2)

    asyncio.run(schedule.schedule_investigation(tc))

    assert get_run(tc.state, wedged).status == runs.RunStatus.FAILED


def test_the_lease_is_off_unless_a_deployment_sets_one(
    mock_submit: mock.MagicMock,
    investigation_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shipped off: failing somebody's run is not a default to take on behalf
    of a deployment that never asked for it."""
    _lease(monkeypatch, minutes=0)
    tc = StateContext()
    wedged = asyncio.run(schedule.schedule_investigation(tc))["run_id"]
    _age_run(investigation_store, wedged, hours=48)

    asyncio.run(schedule.schedule_investigation(tc))

    assert get_run(tc.state, wedged).status == runs.RunStatus.PENDING


def test_the_lease_is_not_part_of_an_agents_configuration() -> None:
    """It fails runs across the whole deployment, so it is set where changing it
    needs deploy access -- the AQuA side of the split, not the object that
    attaching an agent writes."""
    agent_fields = {f.name for f in dataclasses.fields(ObservedAgentConfig)}

    assert "pending_run_lease_minutes" not in agent_fields


def test_failing_the_stale_runs_never_stops_the_submission(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Housekeeping on other runs; the run being asked for comes first."""
    _lease(monkeypatch, minutes=30)

    async def boom(*_a: Any, **_k: Any) -> list[str]:
        await asyncio.sleep(0)  # a real sweep yields to the loop; so does this
        raise RuntimeError("bigquery is having a moment")

    monkeypatch.setattr(runs, "fail_stale_pending_runs", boom)

    result = asyncio.run(schedule.schedule_investigation(StateContext()))

    assert result["status"] == runs.RunStatus.PENDING


def _lease(monkeypatch: pytest.MonkeyPatch, *, minutes: int) -> None:
    """Sets the deploy-time pending run lease duration.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        minutes: Lease duration in minutes.
    """
    monkeypatch.setattr(
        effective_config,
        "env_config",
        make_config(pending_run_lease_minutes=minutes),
    )


def _age_run(store, run_id: str, *, hours: int) -> None:
    """Backdates snapshots of `run_id` by the specified number of hours.

    Args:
        store: Mock investigation store containing snapshots.
        run_id: Investigation run identifier to age.
        hours: Number of hours to offset `updated_at`.
    """
    when = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=hours)).isoformat()
    for row in store.rows:
        if row["run_id"] == run_id:
            row["updated_at"] = when


def _window_span(record: runs.InvestigationRecord) -> dt.timedelta:
    """Calculates duration of the run's evaluation window.

    Args:
        record: Investigation record with ISO timestamp bounds.

    Returns:
        Timedelta span between window start and end.
    """
    return dt.datetime.fromisoformat(
        record.window_end
    ) - dt.datetime.fromisoformat(record.window_start)


# --- _launch_investigation seam (tool + ambient handler) ---------------- #


def test_launch_investigation_ambient_keeps_an_explicit_key(
    mock_submit: mock.MagicMock,
) -> None:
    ctx = CallbackContext(state={})
    result = asyncio.run(
        schedule._launch_investigation(
            ctx, idempotency_key="trigger-7", ambient=True
        )
    )
    record = get_run(ctx.state, result["run_id"])
    assert record.idempotency_key == "trigger-7"
    assert result["idempotency_key"] == "trigger-7"


def test_launch_investigation_scheduled_derives_a_clock_key(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = dt.datetime(2026, 1, 1, 0, 40, 0, tzinfo=dt.UTC)
    ctx = CallbackContext(state={})

    monkeypatch.setattr(schedule, "_read_utc_clock", lambda: base)
    r1 = asyncio.run(schedule._launch_investigation(ctx, ambient=True))
    rec1 = get_run(ctx.state, r1["run_id"])
    assert rec1.idempotency_key is not None
    assert rec1.idempotency_key.startswith(f"{rec1.observed_agent_name}:")

    monkeypatch.setattr(
        schedule, "_read_utc_clock", lambda: base + dt.timedelta(seconds=30)
    )
    r2 = asyncio.run(schedule._launch_investigation(ctx, ambient=True))
    assert (
        get_run(ctx.state, r2["run_id"]).idempotency_key
        == rec1.idempotency_key  # gitleaks:allow
    )

    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: base + dt.timedelta(seconds=schedule.DEDUP_GRAIN_SECONDS),
    )
    r3 = asyncio.run(schedule._launch_investigation(ctx, ambient=True))
    assert (
        get_run(ctx.state, r3["run_id"]).idempotency_key != rec1.idempotency_key
    )


def test_launch_investigation_anchor_keys_on_the_sender_clock(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every attempt at one fire carries the same schedule time, so all of them
    # derive one key and the retries dedup against the first.
    ctx = CallbackContext(state={})
    anchor = dt.datetime(2026, 1, 2, 3, 40, tzinfo=dt.UTC)

    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 1, 2, 3, 41, tzinfo=dt.UTC),
    )
    first = asyncio.run(
        schedule._launch_investigation(ctx, ambient=True, anchor=anchor)
    )

    # A retry an hour later: the clock has moved on, the schedule time has not.
    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 1, 2, 4, 41, tzinfo=dt.UTC),
    )
    retry = asyncio.run(
        schedule._launch_investigation(ctx, ambient=True, anchor=anchor)
    )

    key = get_run(ctx.state, first["run_id"]).idempotency_key
    assert key == get_run(ctx.state, retry["run_id"]).idempotency_key
    assert key is not None


def test_launch_investigation_adhoc_uses_day_window_and_no_keys(
    mock_submit: mock.MagicMock,
) -> None:
    # No ambient params (the tool's call shape): day-based window, no keys.
    ctx = StateContext()
    result = asyncio.run(schedule._launch_investigation(ctx))
    record = get_run(ctx.state, result["run_id"])
    assert _window_span(record) == dt.timedelta(days=7)
    assert record.idempotency_key is None


def test_schedule_tool_stays_adhoc(mock_submit: mock.MagicMock) -> None:
    # Regression guard: the public LLM tool must keep the pre-seam behavior --
    # a day-based window and no idempotency key.
    tc = StateContext()
    result = asyncio.run(schedule.schedule_investigation(tc))
    record = get_run(tc.state, result["run_id"])
    assert _window_span(record) == dt.timedelta(days=7)
    assert record.idempotency_key is None
    assert result["idempotency_key"] is None


# --- submit_investigation (self-query) ------------------------------------ #


def _session_tool_context() -> ToolContext:
    """Creates a ToolContext with distinct, assertable session identifiers.

    Returns:
        Configured ToolContext test fixture.
    """
    return ToolContext(
        app_name="engine-from-session", user_id="u1", session_id="sess-1"
    )


@pytest.fixture
def fake_agentplatform(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Stub `agentplatform.Client` so submit() makes no real calls."""
    job = mock.MagicMock()
    job.job_name = "projects/p/locations/us-central1/operations/op-1"
    client = mock.MagicMock()
    client.agent_engines.run_query_job.return_value = job

    monkeypatch.setattr(
        schedule.agentplatform, "Client", mock.MagicMock(return_value=client)
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "12345")
    return client


def test_submit_builds_self_query_with_keyword(
    fake_agentplatform: mock.MagicMock,
) -> None:
    tc = _session_tool_context()
    result = schedule.submit_investigation("abcd1234", tc)

    assert result["job_name"].endswith("op-1")
    call = fake_agentplatform.agent_engines.run_query_job.call_args
    # Targets THIS engine (self-query) using the injected engine id.
    assert call.kwargs["name"].endswith("/reasoningEngines/12345")
    query = json.loads(call.kwargs["config"]["query"])
    msg = query["input"]["message"]
    assert msg == f"{schedule.RUN_KEYWORD} abcd1234"
    # Reuses the original session so the durable job writes to the same state.
    assert query["input"]["session_id"] == "sess-1"
    assert query["input"]["user_id"] == "u1"


def test_submit_falls_back_to_session_app_name(
    fake_agentplatform: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    schedule.submit_investigation("r1", _session_tool_context())
    name = fake_agentplatform.agent_engines.run_query_job.call_args.kwargs[
        "name"
    ]
    assert name.endswith("/reasoningEngines/engine-from-session")


def test_submit_without_a_session_lets_the_job_open_one(
    fake_agentplatform: mock.MagicMock,
) -> None:
    # An HTTP trigger has no session to reuse. Omitting `session_id` leaves
    # `async_stream_query` to create one; naming a session that does not exist
    # would let this call succeed and kill the job later.
    schedule.submit_investigation("abcd1234", RouteContext())

    call = fake_agentplatform.agent_engines.run_query_job.call_args
    query = json.loads(call.kwargs["config"]["query"])
    assert query["input"] == {
        "message": f"{schedule.RUN_KEYWORD} abcd1234",
        "user_id": "ambient",
    }
    assert call.kwargs["name"].endswith("/reasoningEngines/12345")


def test_submit_reads_session_from_callback_context(
    fake_agentplatform: mock.MagicMock,
) -> None:
    # The scheduling tool and `before_agent` both reach the session via the
    # public `.session` accessor, exactly as for a ToolContext (both are the
    # unified ADK `Context`).
    cc = CallbackContext(
        app_name="engine-from-session", user_id="u1", session_id="sess-1"
    )
    result = schedule.submit_investigation("abcd1234", cc)

    assert result["job_name"].endswith("op-1")
    call = fake_agentplatform.agent_engines.run_query_job.call_args
    query = json.loads(call.kwargs["config"]["query"])
    assert query["input"]["message"] == f"{schedule.RUN_KEYWORD} abcd1234"
    assert query["input"]["session_id"] == "sess-1"
    assert query["input"]["user_id"] == "u1"
    assert call.kwargs["name"].endswith("/reasoningEngines/12345")


# --- the inline sweep's thread -------------------------------------------- #


def _sync_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """Configures in-process synchronous investigation execution.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Config instance with sync_investigation enabled.
    """
    cfg = make_config(sync_investigation=True)
    monkeypatch.setattr(effective_config, "load", lambda state: cfg)
    return cfg


def _sweep(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    """Mocks the execution graph for in-process investigation tests.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        fake: Mock function replacing `execute_investigation`.
    """
    from ambient_quality_agent.core.investigation import job_execution

    monkeypatch.setattr(job_execution, "execute_investigation", fake)


def test_schedule_tool_sync_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_config(monkeypatch)

    fake_execute = mock.AsyncMock()
    _sweep(monkeypatch, fake_execute)

    fake_submit = mock.MagicMock()
    monkeypatch.setattr(schedule, "submit_investigation", fake_submit)

    tc = StateContext()
    result = asyncio.run(schedule.schedule_investigation(tc))

    fake_execute.assert_called_once()
    assert fake_execute.call_args.args[0] == result["run_id"]
    fake_submit.assert_not_called()


def test_the_inline_sweep_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_config(monkeypatch)
    seen: dict[str, int] = {}

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        seen["thread"] = threading.get_ident()
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)

    async def main() -> tuple[int, dict[str, Any]]:
        return (
            threading.get_ident(),
            await schedule.schedule_investigation(StateContext()),
        )

    loop_thread, result = asyncio.run(main())
    assert seen["thread"] != loop_thread
    assert result["run_id"]


def test_the_loop_keeps_serving_while_the_inline_sweep_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression the thread exists to prevent.

    ADK calls the workflow's sync nodes on whichever loop awaits them, so a
    sweep awaited inline would hold every API handler for its whole length --
    and `asyncio.create_task` would too, sharing that loop. Here the sweep waits
    for an event that only the loop can set, so it is released promptly if the
    loop kept turning and times out if it did not.
    """
    _sync_config(monkeypatch)
    inside = threading.Event()
    release = threading.Event()
    blocked: list[bool] = []

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        inside.set()
        # Long enough that only a blocked loop runs it out, not a loaded
        # runner; a working loop releases it at once.
        blocked.append(not release.wait(timeout=30))
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)

    async def main() -> dict[str, Any]:
        sweep = asyncio.create_task(
            schedule.schedule_investigation(StateContext())
        )
        while not inside.is_set():
            await asyncio.sleep(0.01)
        release.set()
        return await sweep

    assert asyncio.run(main())["run_id"]
    assert blocked == [False], (
        "the loop was blocked for the length of the sweep"
    )


def test_the_launch_waits_and_answers_with_the_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Awaiting the thread is what keeps this true: fire-and-forget would answer
    # `pending`, which is not what the dashboard reads off the response.
    _sync_config(monkeypatch)

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        await runs.update_run(context.state, run_id, status=runs.RunStatus.DONE)
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)
    result = asyncio.run(schedule.schedule_investigation(StateContext()))
    assert result["status"] == runs.RunStatus.DONE


class _AdkStateContext:
    """A context whose `.state` is ADK's own `State`, as a chat turn's is."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.session = None


def test_the_sweep_thread_gets_a_detached_copy_of_the_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADK's `State` records writes as a delta for its owning loop to commit and
    # is not thread-safe, so the thread reads a snapshot: it carries everything
    # the scheduling turn had in state, and its writes reach nobody.
    _sync_config(monkeypatch)
    live = State(value={"seeded_before_the_sweep": {"nested": 5}}, delta={})
    seen: dict[str, Any] = {}

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        seen["state"] = context.state
        seen["session"] = context.session
        context.state["written_on_the_thread"] = True
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)
    asyncio.run(schedule.schedule_investigation(_AdkStateContext(live)))

    assert isinstance(seen["state"], dict)
    assert seen["state"]["seeded_before_the_sweep"] == {"nested": 5}
    assert seen["session"] is None
    assert "written_on_the_thread" not in live


# --- one sweep at a time -------------------------------------------------- #


def _shared_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> locking.InProcessInvestigationLock:
    """Installs an isolated in-process lock instance across test invocations.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Isolated InProcessInvestigationLock instance.
    """
    lock = locking.InProcessInvestigationLock()
    monkeypatch.setattr(locking, "lock_factory", lambda config: lock)
    return lock


def test_a_second_sweep_is_refused_and_answers_with_the_running_one(
    monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    _sync_config(monkeypatch)
    _shared_lock(monkeypatch)
    inside = threading.Event()
    release = threading.Event()

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        inside.set()
        release.wait(timeout=5)
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        first = asyncio.create_task(
            schedule.schedule_investigation(StateContext())
        )
        while not inside.is_set():
            await asyncio.sleep(0.01)
        second = await schedule.schedule_investigation(StateContext())
        release.set()
        return await first, second

    first, second = asyncio.run(main())
    assert second["run_id"] == first["run_id"]
    # And no second `pending` record was minted for it.
    assert {row["run_id"] for row in investigation_store.rows} == {
        first["run_id"]
    }


def test_the_next_sweep_runs_once_the_previous_one_has_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_config(monkeypatch)
    _shared_lock(monkeypatch)

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        return {"status": "done"}

    _sweep(monkeypatch, fake_execute)
    first = asyncio.run(schedule.schedule_investigation(StateContext()))
    second = asyncio.run(schedule.schedule_investigation(StateContext()))
    assert first["run_id"] != second["run_id"]


def test_a_sweep_that_raises_still_gives_the_agent_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_config(monkeypatch)
    _shared_lock(monkeypatch)

    async def fake_execute(run_id: str, context: Any) -> dict[str, Any]:
        raise RuntimeError("the graph fell over")

    _sweep(monkeypatch, fake_execute)
    first = asyncio.run(schedule.schedule_investigation(StateContext()))
    second = asyncio.run(schedule.schedule_investigation(StateContext()))
    assert first["status"] == runs.RunStatus.FAILED
    assert second["run_id"] != first["run_id"]


def test_the_durable_path_is_left_unguarded(
    mock_submit: mock.MagicMock,
) -> None:
    # Two clicks already start two durable sweeps: each runs as its own Agent
    # Engine execution, so refusing the second here would refuse a launch on the
    # strength of a sweep this process cannot see.
    first = asyncio.run(schedule.schedule_investigation(StateContext()))
    second = asyncio.run(schedule.schedule_investigation(StateContext()))
    assert first["run_id"] != second["run_id"]
    assert mock_submit.call_count == 2


# --- where the next window starts ----------------------------------------- #


def test_a_mark_older_than_the_lookback_is_clamped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    monkeypatch.setattr(schedule, "_read_utc_clock", lambda: now)

    with caplog.at_level("WARNING"):
        start, end, _ = schedule.derive_window_and_budget(
            make_config(), since="2026-01-01T00:00:00+00:00"
        )

    assert start == (now - dt.timedelta(days=7)).isoformat()
    assert end == now.isoformat()
    assert any("beyond the 7-day lookback" in r.message for r in caplog.records)


def test_a_mark_inside_the_lookback_is_used_as_it_is(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    monkeypatch.setattr(schedule, "_read_utc_clock", lambda: now)
    since = (now - dt.timedelta(days=2)).isoformat()

    with caplog.at_level("WARNING"):
        start, _, _ = schedule.derive_window_and_budget(
            make_config(), since=since
        )

    assert start == since
    assert not [r for r in caplog.records if "lookback" in r.message]


def _seed_run(state: dict, **fields: object) -> None:
    """Stores a completed ambient investigation record with field overrides.

    Args:
        state: Workflow state dictionary.
        **fields: InvestigationRecord field overrides.
    """
    defaults = {
        "status": runs.RunStatus.DONE,
        "observed_agent_name": "test-observed-agent",
        "trigger_type": "scheduled",
        "window_end": "2026-01-01T00:00:00+00:00",
    }
    create_run(state, runs.InvestigationRecord(**{**defaults, **fields}))


def _ambient_window_start(state: dict) -> str:
    """Launches an ambient run and extracts its resolved window start.

    Args:
        state: Workflow state dictionary containing run history.

    Returns:
        ISO timestamp string representing the window start.
    """
    ctx = CallbackContext(state=state)
    result = asyncio.run(schedule._launch_investigation(ctx, ambient=True))
    return get_run(ctx.state, result["run_id"]).window_start


@pytest.fixture
def at_six(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the clock six hours past the seeded runs' window end."""
    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 1, 1, 6, tzinfo=dt.UTC),
    )


def test_an_ambient_run_starts_where_the_last_finished_one_ended(
    mock_submit: mock.MagicMock, at_six: None
) -> None:
    # The wiring, not just the query: without this, dropping the read leaves the
    # suite green.
    state: dict = {}
    _seed_run(state)
    assert _ambient_window_start(state) == "2026-01-01T00:00:00+00:00"


def test_the_newest_finished_run_sets_the_start(
    mock_submit: mock.MagicMock, at_six: None
) -> None:
    state: dict = {}
    _seed_run(state, window_end="2026-01-01T00:00:00+00:00")
    _seed_run(state, window_end="2026-01-01T04:00:00+00:00")
    _seed_run(state, window_end="2026-01-01T02:00:00+00:00")
    assert _ambient_window_start(state) == "2026-01-01T04:00:00+00:00"


@pytest.mark.parametrize(
    ("reason", "fields"),
    [
        ("a failed run", {"status": runs.RunStatus.FAILED}),
        ("a dry run", {"dry_run": True}),
        ("a manual run", {"trigger_type": schedule.MANUAL_TRIGGER_TYPE}),
        ("a custom run", {"trigger_type": schedule.CUSTOM_TRIGGER_TYPE}),
        ("another agent", {"observed_agent_name": "other-agent"}),
    ],
)
def test_a_run_that_covered_nothing_does_not_set_the_start(
    mock_submit: mock.MagicMock, at_six: None, reason: str, fields: dict
) -> None:
    state: dict = {}
    _seed_run(state, **fields)
    start = dt.datetime.fromisoformat(_ambient_window_start(state))
    assert start == dt.datetime(2026, 1, 1, 6, tzinfo=dt.UTC) - dt.timedelta(
        days=7
    ), f"{reason} must not stand for a period being read"


def test_the_dedup_grain_is_five_minutes() -> None:
    assert schedule.DEDUP_GRAIN_SECONDS == 300


# --- custom investigations ------------------------------------------------ #

_OVERRIDES = CustomOverrides(
    selector_sql="SELECT session_id FROM t", session_review_focus="refund flows"
)


def _custom(ctx: Any, **kwargs: Any) -> dict[str, Any]:
    """Launch a custom investigation with a sample explicit time window.

    Args:
        ctx: Launch context.
        **kwargs: Additional keyword arguments passed to _launch_investigation.

    Returns:
        Formatted run result dictionary.
    """
    return asyncio.run(
        schedule._launch_investigation(
            ctx,
            custom_overrides=_OVERRIDES,
            window_start="2026-02-10T13:00:00+00:00",
            window_end="2026-02-10T17:00:00+00:00",
            **kwargs,
        )
    )


def test_a_custom_run_keeps_the_window_it_was_given(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify custom runs retain explicit time windows without lookback clamping."""
    monkeypatch.setattr(
        schedule,
        "_read_utc_clock",
        lambda: dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    )
    ctx = StateContext()

    record = get_run(ctx.state, _custom(ctx)["run_id"])

    assert record.window_start == "2026-02-10T13:00:00+00:00"
    assert record.window_end == "2026-02-10T17:00:00+00:00"


def test_a_custom_run_still_takes_its_budget_from_the_cap(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify custom runs derive metric budgets from the evaluation cap."""
    monkeypatch.setattr(effective_config, "load", lambda state: _eval_config())
    ctx = StateContext()

    record = get_run(ctx.state, _custom(ctx)["run_id"])

    # Evaluation cap of 100 divided across 2 metrics yields 50 per metric.
    assert record.budget_per_metric == 50


def test_a_custom_run_stores_its_overrides_and_trigger_type(
    mock_submit: mock.MagicMock,
) -> None:
    ctx = StateContext()

    record = get_run(ctx.state, _custom(ctx)["run_id"])

    assert record.custom_overrides == _OVERRIDES
    assert record.trigger_type == schedule.CUSTOM_TRIGGER_TYPE


def test_an_ordinary_run_stores_no_overrides(
    mock_submit: mock.MagicMock,
) -> None:
    ctx = StateContext()
    result = asyncio.run(schedule._launch_investigation(ctx))
    record = get_run(ctx.state, result["run_id"])
    assert record.custom_overrides is None
    assert record.trigger_type == schedule.MANUAL_TRIGGER_TYPE


def test_a_naive_window_is_refused(mock_submit: mock.MagicMock) -> None:
    """Verify timestamps without timezone offsets are rejected to avoid UTC misinterpretation."""
    with pytest.raises(ValueError, match="no time zone"):
        asyncio.run(
            schedule._launch_investigation(
                StateContext(),
                custom_overrides=_OVERRIDES,
                window_start="2026-02-10T13:00:00",
                window_end="2026-02-10T17:00:00+00:00",
            )
        )


def test_an_offset_window_is_stored_in_utc() -> None:
    assert (
        schedule.iso_instant_to_utc(
            "2026-09-15T09:00:00+02:00", label="window_start"
        )
        == "2026-09-15T07:00:00+00:00"
    )


def test_an_unreadable_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        schedule.iso_instant_to_utc("last Tuesday", label="window_start")


def test_half_a_window_is_refused(mock_submit: mock.MagicMock) -> None:
    """Verify providing only one window boundary is rejected."""
    with pytest.raises(ValueError, match="both window_start and window_end"):
        asyncio.run(
            schedule._launch_investigation(
                StateContext(), window_start="2026-02-10T13:00:00+00:00"
            )
        )


def test_a_backwards_window_is_refused(mock_submit: mock.MagicMock) -> None:
    """Verify windows with start timestamp after end timestamp are rejected."""
    with pytest.raises(ValueError, match="must fall before"):
        asyncio.run(
            schedule._launch_investigation(
                StateContext(),
                custom_overrides=_OVERRIDES,
                window_start="2026-02-10T17:00:00+00:00",
                window_end="2026-02-10T13:00:00+00:00",
            )
        )


def test_a_dry_run_cannot_carry_overrides(mock_submit: mock.MagicMock) -> None:
    """Verify combining dry_run with custom overrides is rejected."""
    with pytest.raises(ValueError, match="dry run"):
        _custom(StateContext(), dry_run=True)


def test_a_refused_custom_run_is_not_handed_someone_elses_sweep(
    monkeypatch: pytest.MonkeyPatch, investigation_store: Any
) -> None:
    """Verify custom runs return an error instead of attaching to an unrelated running sweep."""
    cfg = _sync_config(monkeypatch)
    lock = _shared_lock(monkeypatch)
    lock.acquire(cfg.observed_agent_name, "someone-elses-run")

    result = _custom(StateContext())

    assert "not started" in result["error"]
    assert result["running_run_id"] == "someone-elses-run"
    assert "run_id" not in result
    assert investigation_store.rows == []


def test_a_refused_ambient_run_still_answers_with_the_running_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ambient launches continue returning the active sweep when concurrency is claimed."""
    cfg = _sync_config(monkeypatch)
    lock = _shared_lock(monkeypatch)
    lock.acquire(cfg.observed_agent_name, "someone-elses-run")

    result = asyncio.run(
        schedule._launch_investigation(StateContext(), ambient=True)
    )

    assert result["run_id"] == "someone-elses-run"


def test_a_custom_run_is_refused_once_too_many_are_in_flight(
    mock_submit: mock.MagicMock,
) -> None:
    """Verify custom runs are rejected when concurrent in-flight runs reach MAX_RUNS_IN_FLIGHT."""
    ctx = StateContext()
    for _ in range(schedule.MAX_RUNS_IN_FLIGHT):
        create_run(
            ctx.state,
            runs.InvestigationRecord(
                status=runs.RunStatus.RUNNING,
                observed_agent_name="test-observed-agent",
            ),
        )

    result = _custom(ctx)

    assert "in flight" in result["error"]
    assert result["runs_in_flight"] == schedule.MAX_RUNS_IN_FLIGHT
    mock_submit.assert_not_called()


def test_finished_runs_do_not_count_against_the_limit(
    mock_submit: mock.MagicMock,
) -> None:
    ctx = StateContext()
    for _ in range(schedule.MAX_RUNS_IN_FLIGHT + 2):
        create_run(
            ctx.state,
            runs.InvestigationRecord(
                status=runs.RunStatus.DONE,
                observed_agent_name="test-observed-agent",
            ),
        )

    assert _custom(ctx)["run_id"]


def test_an_ambient_run_is_never_refused_for_being_busy(
    mock_submit: mock.MagicMock,
) -> None:
    """Verify scheduled ambient runs are not blocked by in-flight run count limits."""
    ctx = CallbackContext(state={})
    for _ in range(schedule.MAX_RUNS_IN_FLIGHT + 1):
        create_run(
            ctx.state,
            runs.InvestigationRecord(
                status=runs.RunStatus.RUNNING,
                observed_agent_name="test-observed-agent",
            ),
        )

    assert asyncio.run(schedule._launch_investigation(ctx, ambient=True))[
        "run_id"
    ]


# --- a run an observed-agent update scheduled ------------------------------ #


def _seed_scheduled_run(
    ctx: Any, run_id: str, *, minutes_overdue: int = 0
) -> runs.InvestigationRecord:
    """Records the scheduled run an observed-agent update leaves.

    Args:
        ctx: Context whose state resolves the store.
        run_id: ID the update derived from its audit event.
        minutes_overdue: Minutes since the due time; negative for not yet due.

    Returns:
        The stored scheduled record.
    """
    due = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes_overdue)
    return create_run(
        ctx.state,
        runs.InvestigationRecord(
            run_id=run_id,
            status=runs.RunStatus.SCHEDULED,
            observed_agent_name="test-observed-agent",
            trigger_type="task_fire",
            idempotency_key="op-1",
            due_at=due.isoformat(),
        ),
    )


def test_the_fired_trigger_advances_the_scheduled_record(
    mock_submit: mock.MagicMock, investigation_store: Any
) -> None:
    """One redeploy reads as one run, from the update arriving to the result."""
    ctx = CallbackContext(state={})
    scheduled = _seed_scheduled_run(ctx, "sched01")

    result = asyncio.run(
        schedule._launch_investigation(
            ctx,
            idempotency_key="op-1",
            ambient=True,
            trigger_type="task_fire",
            scheduled_run_id="sched01",
        )
    )

    assert result["run_id"] == "sched01"
    assert {row["run_id"] for row in investigation_store.rows} == {"sched01"}
    record = get_run(ctx.state, "sched01")
    assert record.status == runs.RunStatus.PENDING
    assert record.due_at == scheduled.due_at
    # The window is derived now, when the trigger fires, so it holds the new
    # revision's traces.
    assert record.window_start and record.window_end
    assert mock_submit.call_args.args[0] == "sched01"


def test_a_redelivered_trigger_does_not_rewrite_a_run_that_moved_on(
    mock_submit: mock.MagicMock,
) -> None:
    """The record already started; the duplicate gets an ID of its own and is
    left for the idempotency marker to skip."""
    ctx = CallbackContext(state={})
    _seed_scheduled_run(ctx, "sched01")
    asyncio.run(runs.update_run(ctx.state, "sched01", status="done"))

    result = asyncio.run(
        schedule._launch_investigation(
            ctx,
            idempotency_key="op-1",
            ambient=True,
            trigger_type="task_fire",
            scheduled_run_id="sched01",
        )
    )

    assert result["run_id"] != "sched01"
    assert get_run(ctx.state, "sched01").status == runs.RunStatus.DONE


def test_a_submission_fails_the_scheduled_runs_whose_trigger_was_lost(
    mock_submit: mock.MagicMock,
) -> None:
    ctx = CallbackContext(state={})
    lost_age = schedule.SCHEDULED_RUN_GRACE_MINUTES + 30
    _seed_scheduled_run(ctx, "lost0001", minutes_overdue=lost_age)
    _seed_scheduled_run(ctx, "wait0001", minutes_overdue=-10)

    asyncio.run(schedule.schedule_investigation(ctx))

    assert get_run(ctx.state, "lost0001").status == runs.RunStatus.FAILED
    assert get_run(ctx.state, "wait0001").status == runs.RunStatus.SCHEDULED


def test_a_late_trigger_still_advances_its_own_record(
    mock_submit: mock.MagicMock,
) -> None:
    """The overdue sweep runs in the same submission, and must not fail the
    run whose trigger is the one arriving."""
    ctx = CallbackContext(state={})
    late_age = schedule.SCHEDULED_RUN_GRACE_MINUTES + 30
    _seed_scheduled_run(ctx, "late0001", minutes_overdue=late_age)

    result = asyncio.run(
        schedule._launch_investigation(
            ctx,
            idempotency_key="op-1",
            ambient=True,
            trigger_type="task_fire",
            scheduled_run_id="late0001",
        )
    )

    assert result["run_id"] == "late0001"
    assert get_run(ctx.state, "late0001").status == runs.RunStatus.PENDING


def test_a_scheduled_run_whose_trigger_finds_a_sweep_running_is_skipped(
    mock_submit: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left scheduled, it would wait for a trigger that has come and gone."""
    lock = _shared_lock(monkeypatch)
    lock.acquire("test-observed-agent", "holder01")
    ctx = CallbackContext(state={})
    _seed_scheduled_run(ctx, "sched01")

    result = asyncio.run(
        schedule._launch_investigation(
            ctx,
            idempotency_key="op-1",
            ambient=True,
            trigger_type="task_fire",
            scheduled_run_id="sched01",
        )
    )

    assert result["run_id"] == "holder01"
    record = get_run(ctx.state, "sched01")
    assert record.status == runs.RunStatus.SKIPPED
    assert "holder01" in (record.error or "")
    mock_submit.assert_not_called()
