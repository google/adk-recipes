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

"""Tests for execution: running the graph and driving scheduled runs.

`run_investigation_graph` runs the `investigation_workflow` graph and
summarizes its results.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core.investigation import job_execution as execute
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.investigations.models import CustomOverrides

from .conftest import (
    FakeGcsClient,
    StateContext,
    create_run,
    get_run,
    make_config,
)


def _config(**overrides: Any) -> dict[str, Any]:
    """Serializes test configuration to a JSON dictionary.

    Args:
        **overrides: Configuration parameter overrides.

    Returns:
        Dictionary representation of the configured options.
    """
    return dataclasses.asdict(make_config(**overrides))


_WINDOW = {
    "window_start": "2026-01-01T00:00:00+00:00",
    "window_end": "2026-01-08T00:00:00+00:00",
    "budget_per_metric": 50,
}


# --- run_investigation_graph ---------------------------------------------- #


def test_run_investigation_returns_done_summary(
    patched_factories: dict[str, Any],
) -> None:
    # Metric scoring runs both evaluation scopes, which the summary asserts on.
    result = asyncio.run(
        execute.run_investigation_graph(
            _config(quality_analysis_mode="eval_service"), **_WINDOW
        )
    )
    assert result["status"] == "done"
    summary = result["summary"]
    assert summary["observed_agent_name"] == "test-agent"
    assert summary["multi_turn_pages_evaluated"] == 1
    assert summary["single_turn_pages_evaluated"] == 1
    # The summary carries the workflow events (progress snippets), including the
    # insight_correlation node's per-sweep summary, so the orchestrator can
    # surface what the run did. The run's findings live in the insight store.
    events = summary["events"]
    assert any("MULTI_TURN evaluation" in e for e in events)
    assert any("SINGLE_TURN evaluation" in e for e in events)
    assert any("Insights" in e for e in events)
    # The eval-outcome tally rides along (fake evaluator scores nothing -> zeros).
    assert summary["metrics_passed"] == 0
    assert summary["metrics_failed"] == 0
    assert summary["metrics_errored"] == 0


def test_dry_run_shrinks_budget(patched_factories: dict[str, Any]) -> None:
    # dry_run re-derives the window/budget from the shrunken cap, overriding
    # whatever was passed in.
    result = asyncio.run(
        execute.run_investigation_graph(_config(), **_WINDOW, dry_run=True)
    )
    # Session review takes the whole cap rather than a per-metric slice.
    assert result["status"] == "done"
    assert result["summary"]["budget_per_metric"] == 2


def test_failure_path_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from ambient_quality_agent.core.nodes import _common

    def _boom(_ctx: Any) -> Any:
        raise RuntimeError("fetcher exploded")

    monkeypatch.setattr(_common, "fetcher_factory", _boom)
    result = asyncio.run(execute.run_investigation_graph(_config(), **_WINDOW))
    assert result["status"] == "failed"
    assert "fetcher exploded" in result["error"]


# --- custom investigations ------------------------------------------------ #


_OVERRIDES = CustomOverrides(
    selector_sql="SELECT session_id FROM t", session_review_focus="refund flows"
)


def _seeded_state(
    monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> dict[str, Any]:
    """Capture initial workflow state passed to a graph execution.

    Args:
        monkeypatch: Pytest monkeypatch fixture to mock the Runner.
        **kwargs: Additional keyword arguments passed to run_investigation_graph.

    Returns:
        Dictionary of seeded state captured during the run.
    """
    seen: dict[str, Any] = {}

    class CapturingRunner:
        def __init__(
            self, *, app_name: str, node: Any, session_service: Any
        ) -> None:
            self.session_service = session_service

        async def run_async(
            self, *, user_id: str, session_id: str, new_message: Any
        ) -> Any:
            session = await self.session_service.get_session(
                app_name=execute._RUN_APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )
            seen.update(session.state)
            return
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(execute, "Runner", CapturingRunner)
    assert (
        asyncio.run(
            execute.run_investigation_graph(_config(), **_WINDOW, **kwargs)
        )["status"]
        == "done"
    )
    return seen


def test_the_overrides_reach_the_graph_as_flat_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify custom overrides and trigger type are populated into flat workflow state."""
    state = _seeded_state(
        monkeypatch, custom_overrides=_OVERRIDES, trigger_type="custom"
    )

    assert state["selector_sql"] == _OVERRIDES.selector_sql
    assert state["session_review_focus"] == _OVERRIDES.session_review_focus
    assert state["trigger_type"] == "custom"


def test_an_ambient_sweep_seeds_the_overrides_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify override keys default to empty strings during ambient runs."""
    state = _seeded_state(monkeypatch)

    assert state["selector_sql"] == ""
    assert state["session_review_focus"] == ""


def test_the_workflow_state_declares_what_is_seeded() -> None:
    fields = WorkflowState.model_fields
    for name in ("selector_sql", "session_review_focus", "trigger_type"):
        assert fields[name].default == ""


def test_a_dry_run_with_overrides_fails_the_run() -> None:
    """Verify dry runs reject custom overrides because dry runs re-derive window and budget."""
    result = asyncio.run(
        execute.run_investigation_graph(
            _config(), **_WINDOW, dry_run=True, custom_overrides=_OVERRIDES
        )
    )

    assert result["status"] == "failed"
    assert "dry run" in result["error"]


def test_a_custom_run_is_not_worth_an_email() -> None:
    """Verify completed custom runs suppress email notifications."""
    counters = {"insights_created": 3}

    assert execute._should_notify(runs.RunStatus.DONE, counters, "custom") == (
        False,
        "",
    )
    # Verify non-custom triggers retain standard notification behavior.
    assert execute._should_notify(runs.RunStatus.DONE, counters, "scheduled")[0]


def test_a_failed_custom_run_is_not_worth_an_email() -> None:
    assert not execute._should_notify(runs.RunStatus.FAILED, {}, "custom")[0]


# --- _summarize -------------------------------------------------------- #


def test_summarize_carries_events_and_page_counts() -> None:
    # The summary distills the terminal state into the events (which carry the
    # run's findings) plus the page counts.
    state = {
        "multi_turn_pages_evaluated": 2,
        "single_turn_pages_evaluated": 1,
        "progress_log": ["**Insights:** 2 new, 1 recurring.\n"],
    }
    summary = execute._summarize(state, make_config())
    assert summary["multi_turn_pages_evaluated"] == 2
    assert summary["single_turn_pages_evaluated"] == 1
    assert summary["events"] == ["**Insights:** 2 new, 1 recurring.\n"]


def test_summarize_defaults_events_to_empty() -> None:
    # A run with no progress log yields an empty events list, never a missing
    # key, so the orchestrator can rely on it being present.
    assert execute._summarize({}, make_config())["events"] == []


def test_summarize_carries_the_goal_the_review_ran_with() -> None:
    # What an A/B comparison groups runs by; empty when no review ran.
    goal = {
        "version": "0123456789ab",
        "source": "saved",
        "prompt_sha256": "f" * 64,
    }
    assert execute._summarize({"goal": goal}, make_config())["goal"] == goal
    assert execute._summarize({}, make_config())["goal"] == {}


def test_summarize_carries_metric_counts() -> None:
    # The pass/fail/error tally computed by the eval nodes is surfaced
    # for the investigations list; rca_status is gone and stays absent.
    state = {"eval_outcome_counts": {"passed": 5, "failed": 2, "errored": 1}}
    summary = execute._summarize(state, make_config())
    assert summary["metrics_passed"] == 5
    assert summary["metrics_failed"] == 2
    assert summary["metrics_errored"] == 1
    assert "rca_status" not in summary


def test_summarize_defaults_metric_counts_to_zero() -> None:
    # Absent counts surface as zeros, never missing keys, so the dashboard can
    # rely on them being present.
    summary = execute._summarize({}, make_config())
    assert summary["metrics_passed"] == 0
    assert summary["metrics_failed"] == 0
    assert summary["metrics_errored"] == 0


# --- execute_investigation (registry transitions) --------------------- #


def test_execute_investigation_drives_run_to_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_graph = mock.AsyncMock(
        return_value={"status": "done", "summary": {"k": "v"}}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)

    state: dict[str, Any] = {}
    record = create_run(
        state,
        runs.InvestigationRecord(
            status=runs.RunStatus.PENDING, effective_config={}
        ),
    )
    ctx = StateContext(state)

    result = asyncio.run(execute.execute_investigation(record.run_id, ctx))

    assert result["status"] == "done"
    persisted = get_run(state, record.run_id)
    assert persisted.status == runs.RunStatus.DONE
    assert persisted.summary == {"k": "v"}


def test_run_investigation_threads_run_id_to_terminal_state(
    patched_factories: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The seeded run_id survives the whole graph and is present in the terminal
    # state. `_summarize` is the single place handed the final state, so snapshot
    # it there to observe what the graph ended with.
    seen: dict[str, Any] = {}
    original = execute._summarize

    def _spy(state: dict[str, Any], config: Any) -> dict[str, Any]:
        seen["run_id"] = state.get("run_id")
        return original(state, config)

    monkeypatch.setattr(execute, "_summarize", _spy)

    result = asyncio.run(
        execute.run_investigation_graph(
            _config(), run_id="sweep-123", **_WINDOW
        )
    )

    assert result["status"] == "done"
    assert seen["run_id"] == "sweep-123"


def test_execute_investigation_forwards_run_id_and_retry_reuses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # execute_investigation forwards the record's run_id to the graph, and a
    # retry (same run_id) forwards the SAME id -- which is what makes run-scoped
    # cleanup idempotent across retries.
    fake_graph = mock.AsyncMock(return_value={"status": "done", "summary": {}})
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)

    state: dict[str, Any] = {}
    record = _pending_run(state)
    ctx = StateContext(state)

    asyncio.run(execute.execute_investigation(record.run_id, ctx))
    asyncio.run(execute.execute_investigation(record.run_id, ctx))

    assert fake_graph.await_count == 2
    first_kwargs = fake_graph.await_args_list[0].kwargs
    second_kwargs = fake_graph.await_args_list[1].kwargs
    assert first_kwargs["run_id"] == record.run_id
    assert second_kwargs["run_id"] == record.run_id


def test_execute_investigation_records_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_graph = mock.AsyncMock(
        return_value={"status": "failed", "error": "boom"}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)

    state: dict[str, Any] = {}
    record = create_run(
        state, runs.InvestigationRecord(status=runs.RunStatus.PENDING)
    )

    result = asyncio.run(
        execute.execute_investigation(record.run_id, StateContext(state))
    )

    assert result["status"] == "failed"
    assert get_run(state, record.run_id).status == runs.RunStatus.FAILED


def test_execute_investigation_unknown_run() -> None:
    result = asyncio.run(
        execute.execute_investigation("nope", StateContext({}))
    )
    assert result["status"] == "failed"
    assert "unknown run" in result["error"]


# --- execute_investigation (idempotency marker, Phase 2.4) ------------ #


def _pending_run(
    state: dict[str, Any], **fields: Any
) -> runs.InvestigationRecord:
    """Creates a PENDING investigation record with valid default effective configuration.

    Args:
        state: State dictionary to populate with the record.
        **fields: InvestigationRecord field overrides.

    Returns:
        Created InvestigationRecord instance.
    """
    fields.setdefault("effective_config", _config())
    return create_run(
        state, runs.InvestigationRecord(status=runs.RunStatus.PENDING, **fields)
    )


def test_execute_investigation_claims_marker_then_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ambient run (idempotency key + jobs bucket) claims its marker and then
    # runs the graph -- the happy path is unchanged by the guard.
    fake_graph = mock.AsyncMock(
        return_value={"status": "done", "summary": {"k": "v"}}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)
    fake_gcs = FakeGcsClient()
    monkeypatch.setattr(execute.gcs, "create_storage_client", lambda: fake_gcs)

    state: dict[str, Any] = {}
    record = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="msg-1",
    )

    result = asyncio.run(
        execute.execute_investigation(record.run_id, StateContext(state))
    )

    assert result["status"] == "done"
    fake_graph.assert_awaited_once()
    assert get_run(state, record.run_id).status == runs.RunStatus.DONE
    # The marker was claimed under the hashed key path (the raw key is sha256'd
    # into the object name to bound length and sanitize the charset).
    object_name = (
        f"aqa-idempotency-keys/{hashlib.sha256(b'msg-1').hexdigest()}.json"
    )
    assert ("jobs", object_name) in fake_gcs.objects


def test_execute_investigation_skips_duplicate_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redelivered trigger (fresh run_id, SAME idempotency key) finds the marker
    # already present, so its run is SKIPPED and the graph runs only once.
    fake_graph = mock.AsyncMock(return_value={"status": "done", "summary": {}})
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)
    fake_gcs = FakeGcsClient()  # one shared marker store across both runs
    monkeypatch.setattr(execute.gcs, "create_storage_client", lambda: fake_gcs)

    state: dict[str, Any] = {}
    ctx = StateContext(state)
    first = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="dup",
    )
    second = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="dup",
    )
    assert first.run_id != second.run_id

    first_result = asyncio.run(execute.execute_investigation(first.run_id, ctx))
    second_result = asyncio.run(
        execute.execute_investigation(second.run_id, ctx)
    )

    assert first_result["status"] == "done"
    assert second_result == {"status": "skipped", "reason": "duplicate"}
    assert get_run(state, second.run_id).status == runs.RunStatus.SKIPPED
    fake_graph.assert_awaited_once()  # only the first run reached the graph


def test_execute_investigation_crash_retry_reruns_despite_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Crash-recovery: when a Cloud Run Job task crashes mid-run, Cloud Run retries
    # the SAME task, so the marker the retry finds is its own (left by the crashed
    # attempt), not a duplicate delivery. On a retry attempt
    # (`CLOUD_RUN_TASK_ATTEMPT` > 0) the guard must PROCEED and run the graph
    # rather than skip -- otherwise the window is silently dropped.
    fake_graph = mock.AsyncMock(
        return_value={"status": "done", "summary": {"k": "v"}}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)
    # Marker already present (the crashed earlier attempt claimed it first).
    monkeypatch.setattr(execute.gcs, "claim_once", lambda *_: False)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")

    state: dict[str, Any] = {}
    record = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="msg-1",
    )

    result = asyncio.run(
        execute.execute_investigation(record.run_id, StateContext(state))
    )

    # Proceeded past the guard (RUNNING -> DONE) instead of being SKIPPED.
    assert result["status"] == "done"
    fake_graph.assert_awaited_once()
    assert get_run(state, record.run_id).status == runs.RunStatus.DONE


def test_execute_investigation_no_idempotency_key_skips_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An ad-hoc run (no idempotency key) never touches GCS: the guard is bypassed
    # and behavior is exactly as before Phase 2.4.
    fake_graph = mock.AsyncMock(return_value={"status": "done", "summary": {}})
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)

    def _explode() -> Any:
        raise AssertionError(
            "claim_once must not run without an idempotency key"
        )

    monkeypatch.setattr(execute.gcs, "create_storage_client", _explode)

    state: dict[str, Any] = {}
    record = _pending_run(
        state, effective_config=_config(jobs_gcs_bucket="jobs")
    )

    result = asyncio.run(
        execute.execute_investigation(record.run_id, StateContext(state))
    )

    assert result["status"] == "done"
    fake_graph.assert_awaited_once()


def test_execute_investigation_no_bucket_skips_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local run (idempotency key but no jobs bucket) has no marker store, so the
    # guard is skipped gracefully and the graph still runs.
    fake_graph = mock.AsyncMock(return_value={"status": "done", "summary": {}})
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)

    def _explode() -> Any:
        raise AssertionError("no jobs bucket => no GCS marker")

    monkeypatch.setattr(execute.gcs, "create_storage_client", _explode)

    state: dict[str, Any] = {}
    record = _pending_run(state, idempotency_key="k")  # _config() has no bucket

    result = asyncio.run(
        execute.execute_investigation(record.run_id, StateContext(state))
    )

    assert result["status"] == "done"
    fake_graph.assert_awaited_once()


def test_execute_investigation_failed_keeps_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A logical failure is terminal for the trigger: the marker PERSISTS (it is
    # never released), so a Pub/Sub redelivery of the same key dedups (SKIPPED)
    # rather than re-running a window that already failed. Releasing was a no-op --
    # a logical failure completes HTTP 200 (Cloud Run does not retry it) and a
    # crash-retry is handled by the CLOUD_RUN_TASK_ATTEMPT bypass, not by release.
    fake_graph = mock.AsyncMock(
        return_value={"status": "failed", "error": "boom"}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph)
    fake_gcs = FakeGcsClient()
    monkeypatch.setattr(execute.gcs, "create_storage_client", lambda: fake_gcs)

    state: dict[str, Any] = {}
    ctx = StateContext(state)
    record = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="k1",
    )

    result = asyncio.run(execute.execute_investigation(record.run_id, ctx))

    assert result["status"] == "failed"
    assert get_run(state, record.run_id).status == runs.RunStatus.FAILED
    # The marker was NOT released: it survives and no delete was recorded.
    object_name = (
        f"aqa-idempotency-keys/{hashlib.sha256(b'k1').hexdigest()}.json"
    )
    assert ("jobs", object_name) in fake_gcs.objects
    assert ("jobs", object_name) not in fake_gcs.deletes

    # A redelivery with the SAME key finds the surviving marker and is SKIPPED
    # (attempt 0), so the already-failed window is not re-run.
    fake_graph_ok = mock.AsyncMock(
        return_value={"status": "done", "summary": {}}
    )
    monkeypatch.setattr(execute, "run_investigation_graph", fake_graph_ok)
    redelivery = _pending_run(
        state,
        effective_config=_config(jobs_gcs_bucket="jobs"),
        idempotency_key="k1",
    )
    redelivery_result = asyncio.run(
        execute.execute_investigation(redelivery.run_id, ctx)
    )

    assert redelivery_result == {"status": "skipped", "reason": "duplicate"}
    assert get_run(state, redelivery.run_id).status == runs.RunStatus.SKIPPED
    fake_graph_ok.assert_not_awaited()  # the redelivery never reached the graph


def test_the_counters_carry_the_insight_tallies_the_alert_reads() -> None:
    """The other half of the contract in `test_insight_correlation`.

    The node records the pair as counters and the alert reads them from the
    record; nothing lifts them into the summary under a second set of names any
    more, so this is the whole path between the two.
    """
    counters = execute._extract_counters(
        {"counters": {"insights_created": 2, "insights_recurring": 1}}
    )

    assert counters["insights_created"] == 2
    assert counters["insights_recurring"] == 1


def test_the_counters_report_no_insights_when_the_node_never_ran() -> None:
    """A run that failed before correlation reports zeros, not missing keys.

    `EXTRACT` copies a missing field into the mail as `NULL_VALUE`, so every
    counter the policy labels on has to be present on every finished run.
    """
    counters = execute._extract_counters({})

    assert counters["insights_created"] == 0
    assert counters["insights_recurring"] == 0
