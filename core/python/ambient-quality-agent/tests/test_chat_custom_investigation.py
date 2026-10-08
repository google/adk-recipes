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

"""Tests for custom investigation tools in the chat agent.

Covers previewing SQL selectors, launching custom investigations, enforcing
retry caps on refused selectors, and emitting validation telemetry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import chat_agent
from ambient_quality_agent.core.investigation import job_scheduling as schedule
from ambient_quality_agent.core.investigation import locking, preview
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.investigations.models import (
    CUSTOM_TRIGGER_TYPE,
    RunStatus,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config

from .conftest import StateContext, get_run, make_config

_SELECTOR = """
SELECT session_id AS target_id
FROM `t`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
"""
"""A selector that passes all validation checks."""

_UNSCOPED_SELECTOR = """
SELECT session_id AS target_id
FROM `t`
WHERE timestamp BETWEEN @window_start AND @window_end
"""
"""A selector omitting @agent_name, rejected during lexical validation."""

_START = "2026-01-01T00:00:00+00:00"
_END = "2026-01-08T00:00:00+00:00"


class _Fetcher:
    """Test telemetry fetcher with configurable precheck results and match counts."""

    def __init__(
        self,
        *,
        precheck: selector.PrecheckResult | None = None,
        matched: int = 0,
    ) -> None:
        self._precheck = precheck or selector.PrecheckResult(
            estimated_bytes=4096
        )
        self._matched = matched
        self.recorder: Any = None
        self.calls: list[str] = []

    def precheck_selector(self, *_: Any, **__: Any) -> selector.PrecheckResult:
        self.calls.append("precheck")
        return self._precheck

    def count_scanned(self, *_: Any, **__: Any) -> int:
        self.calls.append("count_scanned")
        return self._matched

    def submit_query(self, **__: Any) -> str:
        return "job-1"

    def fetch_page(self, **__: Any) -> None:
        self.recorder.flush()


@pytest.fixture
def fetcher(monkeypatch: pytest.MonkeyPatch) -> _Fetcher:
    """Serve all selector checks from one in-memory test fetcher."""
    built = _Fetcher()

    def factory(_config: Any, _selector_sql: str, *, recorder: Any) -> _Fetcher:
        built.recorder = recorder
        return built

    monkeypatch.setattr(preview, "fetcher_factory", factory)
    monkeypatch.setattr(
        preview, "store_factory", lambda _config: mock.MagicMock()
    )
    return built


@pytest.fixture
def mock_submit(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Mock durable job submission to verify launch behavior without running jobs."""
    fake = mock.MagicMock(return_value={"job_name": "op/123"})
    monkeypatch.setattr(schedule, "submit_investigation", fake)
    return fake


def _eval_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Puts the deployment in eval_service mode where sessions are scored rather than reviewed.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    config = make_config(quality_analysis_mode="eval_service")
    monkeypatch.setattr(effective_config, "load", lambda _state: config)


def _preview(context: StateContext, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("selector_sql", _SELECTOR)
    kwargs.setdefault("window_start", _START)
    kwargs.setdefault("window_end", _END)
    return asyncio.run(
        chat_agent.preview_custom_investigation(context, **kwargs)
    )  # type: ignore[arg-type]


def _start(context: StateContext, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("selector_sql", _SELECTOR)
    kwargs.setdefault("window_start", _START)
    kwargs.setdefault("window_end", _END)
    return asyncio.run(chat_agent.start_custom_investigation(context, **kwargs))  # type: ignore[arg-type]


# --- the mode that cannot serve them ---------------------------------------- #


def test_the_preview_refuses_where_sessions_are_scored_rather_than_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = StateContext()
    _eval_service(monkeypatch)

    result = _preview(context)

    assert result == {"error": chat_agent.CUSTOM_INVESTIGATIONS_UNAVAILABLE}
    assert "eval_service" in result["error"]


def test_starting_one_refuses_where_sessions_are_scored_rather_than_reviewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that launching refuses independently without requiring a prior preview."""
    context = StateContext()
    _eval_service(monkeypatch)

    assert _start(context) == {
        "error": chat_agent.CUSTOM_INVESTIGATIONS_UNAVAILABLE
    }


# --- a rejection is data ------------------------------------------------------ #


def test_a_refused_selector_comes_back_with_its_reason(
    fetcher: _Fetcher,
) -> None:
    """Verify selector rejection reasons are returned in the preview response."""
    result = _preview(StateContext(), selector_sql=_UNSCOPED_SELECTOR)

    assert result["rejected"] is True
    assert (
        result["reason"] == selector.RejectionReason.MISSING_AGENT_FILTER.value
    )
    assert result["explanation"]


def test_a_dry_run_rejection_keeps_its_reason_too(
    fetcher: _Fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify BigQuery dry-run rejections format consistently with lexical rejections."""
    monkeypatch.setattr(
        fetcher,
        "_precheck",
        selector.PrecheckResult(
            rejection=selector.Rejection(
                reason=selector.RejectionReason.UNAUTHORIZED_TABLE,
                explanation="the selector reads a table it may not",
            ),
            estimated_bytes=17,
        ),
    )

    result = _preview(StateContext())

    assert result["reason"] == selector.RejectionReason.UNAUTHORIZED_TABLE.value


def test_an_accepted_selector_reports_what_it_matched(
    fetcher: _Fetcher,
) -> None:
    fetcher._matched = 42

    result = _preview(
        StateContext(), session_review_focus="only booking failures"
    )

    assert result["rejected"] is False
    assert result["matched"] == 42
    # Echoed back so the caller can forward the focus filter to launch.
    assert result["session_review_focus"] == "only booking failures"


# --- the retry cap ------------------------------------------------------------ #


def test_the_fourth_refused_preview_samples_nothing(fetcher: _Fetcher) -> None:
    """Verify previews stop sampling after reaching MAX_SELECTOR_ATTEMPTS."""
    context = StateContext()

    counts = [
        _preview(context, selector_sql=_UNSCOPED_SELECTOR)["attempts"]
        for _ in range(chat_agent.MAX_SELECTOR_ATTEMPTS)
    ]
    exhausted = _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    assert counts == [1, 2, 3]
    assert exhausted["attempts_exhausted"] is True
    # Rejection details are preserved so the model can report the error.
    assert (
        exhausted["reason"]
        == selector.RejectionReason.MISSING_AGENT_FILTER.value
    )
    assert str(chat_agent.MAX_SELECTOR_ATTEMPTS) in exhausted["message"]
    assert "count_scanned" not in fetcher.calls


def test_a_corrected_selector_ends_the_run_of_refusals(
    fetcher: _Fetcher,
) -> None:
    """Verify a valid selector resets refusal attempts even after reaching the cap."""
    context = StateContext()
    for _ in range(chat_agent.MAX_SELECTOR_ATTEMPTS):
        _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    recovered = _preview(context)

    assert recovered["rejected"] is False
    assert recovered["attempts_exhausted"] is False
    # Attempt count is reset, so subsequent previews resume sampling.
    assert _preview(context)["matched"] == fetcher._matched


def test_a_timed_out_preview_neither_spends_nor_returns_an_attempt(
    fetcher: _Fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify timed-out previews do not alter the recorded attempt count."""
    monkeypatch.setattr(preview, "DEADLINE_SECONDS", 0.0)
    context = StateContext()
    _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    timed_out = _preview(context)

    assert timed_out["timed_out"] is True
    assert _preview(context, selector_sql=_UNSCOPED_SELECTOR)["attempts"] == 2


def test_an_accepted_selector_returns_the_attempts(fetcher: _Fetcher) -> None:
    """Verify a successful preview resets consecutive refusal attempts to zero."""
    context = StateContext()
    _preview(context, selector_sql=_UNSCOPED_SELECTOR)
    _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    assert _preview(context)["attempts"] == 0
    assert _preview(context, selector_sql=_UNSCOPED_SELECTOR)["attempts"] == 1


def test_one_conversations_refusals_do_not_spend_anothers(
    fetcher: _Fetcher,
) -> None:
    spent = StateContext()
    for _ in range(chat_agent.MAX_SELECTOR_ATTEMPTS):
        _preview(spent, selector_sql=_UNSCOPED_SELECTOR)

    fresh = _preview(StateContext(), selector_sql=_UNSCOPED_SELECTOR)

    assert fresh["attempts"] == 1


def test_a_malformed_request_spends_no_attempt(fetcher: _Fetcher) -> None:
    """Verify request validation errors do not count toward selector refusal attempts."""
    context = StateContext()

    result = _preview(context, window_start="2026-01-01T00:00:00")

    assert "error" in result
    assert "window_start" in result["error"]
    assert context.state.get(chat_agent.SELECTOR_ATTEMPTS_KEY) is None


def test_starting_a_run_with_a_naive_window_is_refused(
    fetcher: _Fetcher,
) -> None:
    result = _start(StateContext(), window_end="2026-01-08T00:00:00")

    assert "error" in result
    assert "window_end" in result["error"]


# --- starting one ------------------------------------------------------------- #


def test_a_started_run_carries_the_overrides_and_the_custom_trigger(
    fetcher: _Fetcher, mock_submit: mock.MagicMock
) -> None:
    context = StateContext()

    result = _start(context, session_review_focus="only booking failures")

    record = get_run(context.state, result["run_id"])
    assert result["status"] == RunStatus.PENDING
    assert record.trigger_type == CUSTOM_TRIGGER_TYPE
    assert record.custom_overrides is not None
    assert record.custom_overrides.selector_sql == _SELECTOR
    assert (
        record.custom_overrides.session_review_focus == "only booking failures"
    )


def test_a_refused_selector_starts_nothing(
    fetcher: _Fetcher, mock_submit: mock.MagicMock
) -> None:
    result = _start(StateContext(), selector_sql=_UNSCOPED_SELECTOR)

    assert result["rejected"] is True
    assert (
        result["reason"] == selector.RejectionReason.MISSING_AGENT_FILTER.value
    )
    assert "run_id" not in result
    mock_submit.assert_not_called()


def test_a_held_agent_is_reported_rather_than_its_running_sweep(
    fetcher: _Fetcher,
    mock_submit: mock.MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify active runs block custom runs rather than returning unmatched sweep results."""
    held = mock.MagicMock()
    held.acquire.return_value = "run-already-running"
    monkeypatch.setattr(locking, "lock_factory", lambda _config: held)

    result = _start(StateContext())

    assert result["running_run_id"] == "run-already-running"
    assert "run_id" not in result
    mock_submit.assert_not_called()


def test_a_busy_deployment_refuses_rather_than_queueing(
    fetcher: _Fetcher,
    mock_submit: mock.MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule, "MAX_RUNS_IN_FLIGHT", 1)
    context = StateContext()
    _start(context)

    result = _start(context)

    assert result["runs_in_flight"] == 1
    assert "run_id" not in result


def test_a_launch_that_raises_is_reported_not_propagated(
    fetcher: _Fetcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        schedule,
        "submit_investigation",
        mock.MagicMock(side_effect=RuntimeError("no")),
    )

    result = _start(StateContext())

    # A run recorded and never submitted is a failure, not a started run.
    assert "no" in result["error"]
    assert "run_id" not in result


# --- what the logs say -------------------------------------------------------- #


def _validation_entries(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, Any]]:
    """Extract structured validation log records emitted during preview execution.

    Args:
        caplog: Pytest log capture fixture.

    Returns:
        List of parsed JSON validation log payloads.
    """
    entries = []
    for record in caplog.records:
        try:
            payload = json.loads(record.getMessage())
        except ValueError:
            continue
        if payload.get("event") == preview.VALIDATION_EVENT:
            entries.append(payload)
    return entries


def test_an_accepted_selector_logs_what_it_is_about_to_scan(
    fetcher: _Fetcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify estimated scan bytes are logged when a selector passes validation."""
    with caplog.at_level(logging.INFO, logger=preview.__name__):
        _preview(StateContext())

    assert _validation_entries(caplog) == [
        {
            "event": preview.VALIDATION_EVENT,
            "agent": mock.ANY,
            "surface": preview.PREVIEW_SURFACE,
            "rejected": False,
            "reason": None,
            "estimated_bytes": 4096,
        }
    ]


def test_a_rejection_logs_its_reason(
    fetcher: _Fetcher, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=preview.__name__):
        _preview(StateContext(), selector_sql=_UNSCOPED_SELECTOR)

    entry = _validation_entries(caplog)[0]
    assert entry["rejected"] is True
    assert (
        entry["reason"] == selector.RejectionReason.MISSING_AGENT_FILTER.value
    )


def test_a_preview_past_the_cap_is_still_logged_as_a_preview(
    fetcher: _Fetcher, caplog: pytest.LogCaptureFixture
) -> None:
    """Attribution by `surface` is what makes previews and launches countable
    apart, and the dry run past the cap is still a preview."""
    context = StateContext()
    for _ in range(chat_agent.MAX_SELECTOR_ATTEMPTS):
        _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    with caplog.at_level(logging.INFO, logger=preview.__name__):
        # The attempts above log too whenever something earlier in the suite has
        # already turned INFO on, so only what follows the clear is measured.
        caplog.clear()
        _preview(context, selector_sql=_UNSCOPED_SELECTOR)

    assert [e["surface"] for e in _validation_entries(caplog)] == [
        preview.PREVIEW_SURFACE
    ]


def test_the_launch_logs_its_own_validation(
    fetcher: _Fetcher,
    mock_submit: mock.MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify launching an investigation records validation with the 'start' surface."""
    with caplog.at_level(logging.INFO, logger=preview.__name__):
        _start(StateContext())

    assert [e["surface"] for e in _validation_entries(caplog)] == [
        preview.START_SURFACE
    ]
