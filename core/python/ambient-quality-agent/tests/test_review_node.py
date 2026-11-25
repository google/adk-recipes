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

"""Unit tests for the review node's page reviews and sweep outcome.

Every model call goes to a fake Gemini client. All session data is synthetic.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import functools
import gc
import threading
import traceback
import weakref
from concurrent import futures
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.core.nodes import _common, review
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
from ambient_quality_agent.tools.ingestion.models import IngestionCounts, Page
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.review import agentic_reviewer
from google.genai import types

from tests.conftest import make_agent_case, make_page
from tests.test_agentic_reviewer import (
    FINDING,
    WORKER,
    FakeClient,
    FakeModels,
    _answer,
    _respond,
)

UNPARSABLE_MARKER = "Answer me in prose."

_CALLER_VALUE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "caller_value", default=""
)


def _review_with(
    reviewer: Any, cases: list[EvalCase]
) -> tuple[review.ReviewEvaluation, dict[str, Any]]:
    """Review one page of `cases` with `reviewer`.

    Args:
        reviewer: Session reviewer run for every session.
        cases: The page's evaluation cases.

    Returns:
        The review evaluation and the workflow state it wrote.
    """
    evaluation = review.ReviewEvaluation(reviewer, InvestigationGoal())
    state: dict[str, Any] = {}
    evaluation.evaluate(state, make_page(cases))
    return evaluation, state


def _build_failing_reviewer(error: Exception) -> Any:
    """Build a session reviewer that raises `error`.

    Args:
        error: The exception every review raises.

    Returns:
        The session reviewer.
    """

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


def test_an_unparsable_reply_alone_does_not_fail_the_sweep() -> None:
    evaluation, state = _review_with(
        _build_failing_reviewer(agentic_reviewer.UnparsableReplyError("prose")),
        [make_agent_case("s1")],
    )

    evaluation.finish(state)

    assert evaluation.outcomes == {"s1": "errored"}


def test_every_review_failing_on_infrastructure_fails_the_sweep() -> None:
    evaluation, state = _review_with(
        _build_failing_reviewer(RuntimeError("model unreachable")),
        [make_agent_case("s1"), make_agent_case("s2")],
    )

    with pytest.raises(RuntimeError, match="model unreachable"):
        evaluation.finish(state)


def test_unparsable_replies_do_not_hide_an_infrastructure_outage() -> None:
    def fail(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
        if eval_case.eval_case_id == "s1":
            raise agentic_reviewer.UnparsableReplyError("prose")
        raise RuntimeError("model unreachable")

    evaluation, state = _review_with(
        fail, [make_agent_case("s1"), make_agent_case("s2")]
    )

    with pytest.raises(RuntimeError, match="model unreachable"):
        evaluation.finish(state)


def test_one_infrastructure_success_keeps_the_sweep() -> None:
    def fail_once(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
        if eval_case.eval_case_id == "s1":
            raise RuntimeError("model unreachable")
        return []

    evaluation, state = _review_with(
        fail_once, [make_agent_case("s1"), make_agent_case("s2")]
    )

    evaluation.finish(state)

    assert evaluation.outcomes == {"s1": "errored", "s2": "passed"}


class ContentRoutedModels(FakeModels):
    """Answers each call from the reviewed session's first message.

    Concurrent reviews reach a shared client in any order, so a script consumed
    in call order cannot tell them apart.
    """

    def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        """Record the request and answer it.

        Args:
            model: Model identifier.
            contents: Request conversation contents.
            config: Request generation configuration.

        Returns:
            Prose for the session holding `UNPARSABLE_MARKER`, and one finding
            for any other session.
        """
        with self.lock:
            self.requests.append((list(contents), config))
        first_text = (contents[0].parts or [types.Part()])[0].text or ""
        if UNPARSABLE_MARKER in first_text:
            return _respond(types.Part(text="Nothing to report, in prose."))
        return _answer([FINDING])


def _build_case(case_id: str, user_text: str) -> EvalCase:
    """Build a one-turn session of the worker agent.

    Args:
        case_id: Evaluation case id.
        user_text: The user's message.

    Returns:
        The evaluation case.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    WORKER: {
                        "agent_id": WORKER,
                        "instruction": "Never refund a shipped order.",
                    }
                },
                "turns": [
                    {
                        "turn_index": 0,
                        "turn_id": "turn-0",
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [{"text": user_text}],
                                },
                            },
                            {
                                "author": WORKER,
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "Refunded."}],
                                },
                            },
                        ],
                    }
                ],
            },
        }
    )


def test_the_node_pool_runs_real_reviews_on_one_shared_client() -> None:
    models = ContentRoutedModels([])
    client = FakeClient([], models=models)
    cases = [
        _build_case("s1", "Refund order 1."),
        _build_case("s2", UNPARSABLE_MARKER),
        _build_case("s3", "Refund order 3."),
        _build_case("s4", "Refund order 4."),
    ]

    evaluation, state = _review_with(
        functools.partial(agentic_reviewer.extract_findings, client=client),
        cases,
    )
    evaluation.finish(state)

    assert evaluation.outcomes == {
        "s1": "failed",
        "s2": "errored",
        "s3": "failed",
        "s4": "failed",
    }
    (finding_set,) = state["finding_sets"]
    assert [
        (finding["trajectory_id"], finding["actual_behavior"])
        for finding in finding_set["findings"]
    ] == [
        (case_id, FINDING["actual_behavior"]) for case_id in ("s1", "s3", "s4")
    ]
    # One answer per parsed session, and the unparsable session's first reply
    # and re-ask.
    assert len(models.requests) == 5


def test_malformed_finding_entries_record_errored_not_passed() -> None:
    malformed = _respond(
        types.Part(text='{"findings": [{"defect": "wrong field name"}]}')
    )
    client = FakeClient([malformed, malformed])

    evaluation, state = _review_with(
        functools.partial(agentic_reviewer.extract_findings, client=client),
        [_build_case("s1", "Refund order 1.")],
    )
    evaluation.finish(state)

    assert evaluation.outcomes == {"s1": "errored"}
    assert state["finding_sets"][0]["findings"] == []


def test_each_review_sees_the_callers_context_variables() -> None:
    seen: list[str] = []

    def record_context(*args: Any, **kwargs: Any) -> Any:
        seen.append(_CALLER_VALUE.get())
        return []

    token = _CALLER_VALUE.set("node span")
    try:
        evaluation, state = _review_with(
            record_context, [make_agent_case("s1"), make_agent_case("s2")]
        )
    finally:
        _CALLER_VALUE.reset(token)
    evaluation.finish(state)

    assert seen == ["node span", "node span"]


def _call_unreachable_model(eval_case: Any) -> Any:
    """Fail the way a model call fails, in a frame that holds the session.

    Args:
        eval_case: The session under review.

    Raises:
        RuntimeError: Always.
    """
    raise RuntimeError("model unreachable")


def _review_unreachable(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
    """Review a session whose model call fails.

    Args:
        eval_case: The session under review.
        *args: Unused.
        **kwargs: Unused.

    Raises:
        RuntimeError: Always.
    """
    return _call_unreachable_model(eval_case)


def _review_past_deadline(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
    """Review a session whose failed model call is raised as another error.

    The frame of the model call is then reachable only through the cause.

    Args:
        eval_case: The session under review.
        *args: Unused.
        **kwargs: Unused.

    Raises:
        TimeoutError: Always, from the model call's `RuntimeError`.
    """
    try:
        return _call_unreachable_model(eval_case)
    except RuntimeError as exc:
        raise TimeoutError("review deadline") from exc


@pytest.mark.parametrize(
    ("reviewer", "error_type"),
    [
        (_review_unreachable, RuntimeError),
        (_review_past_deadline, TimeoutError),
    ],
    ids=["raised", "raised-from-a-cause"],
)
def test_a_failed_review_does_not_keep_its_session_in_memory(
    reviewer: Any, error_type: type[Exception]
) -> None:
    """The evaluation keeps a failed review's error to fail the sweep with. A
    traceback holds the local variables of its frames, the session among them,
    so the error must not carry them for the rest of the sweep."""
    case = make_agent_case("s1")
    session = weakref.ref(case)

    evaluation, state = _review_with(reviewer, [case])
    del case
    with pytest.raises(error_type) as raised:
        evaluation.finish(state)
    gc.collect()

    assert session() is None
    # The error still shows where it came from.
    assert "_call_unreachable_model" in "".join(
        traceback.format_exception(raised.value)
    )


def test_unfinished_reviews_never_exceed_the_review_window_from_rebound_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebinding `REVIEW_CONCURRENCY` at run time sets both the pool's worker
    count and the `2 * REVIEW_CONCURRENCY` window of unfinished reviews."""
    monkeypatch.setattr(review, "REVIEW_CONCURRENCY", 2)
    started = [threading.Event() for _ in range(6)]
    release = [threading.Event() for _ in range(6)]
    submitted_events = [threading.Event() for _ in range(6)]
    lock = threading.Lock()
    running = 0
    max_running = 0
    submitted_count = 0

    class _TrackedPool(futures.ThreadPoolExecutor):
        def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
            nonlocal submitted_count
            future = super().submit(fn, *args, **kwargs)
            with lock:
                index = submitted_count
                submitted_count += 1
            submitted_events[index].set()
            return future

    monkeypatch.setattr(review.futures, "ThreadPoolExecutor", _TrackedPool)

    def review_behind_gate(
        eval_case: Any, *args: Any, **kwargs: Any
    ) -> list[Any]:
        nonlocal running, max_running
        index = int(eval_case.eval_case_id[1:])
        with lock:
            running += 1
            max_running = max(max_running, running)
        started[index].set()
        try:
            assert release[index].wait(timeout=5)
            return []
        finally:
            with lock:
                running -= 1

    evaluation = review.ReviewEvaluation(
        review_behind_gate, InvestigationGoal()
    )
    state: dict[str, Any] = {}
    cases = [make_agent_case(f"s{n}") for n in range(6)]
    driver = threading.Thread(
        target=evaluation.evaluate, args=(state, make_page(cases))
    )
    driver.start()
    try:
        assert started[0].wait(timeout=5)
        assert started[1].wait(timeout=5)
        assert submitted_events[3].wait(timeout=5)
        assert not submitted_events[4].is_set()
        assert max_running == 2

        release[0].set()
        assert submitted_events[4].wait(timeout=5)
        assert started[2].wait(timeout=5)
        assert not submitted_events[5].is_set()

        release[1].set()
        assert submitted_events[5].wait(timeout=5)
        driver.join(timeout=5)
        assert not driver.is_alive()
        assert evaluation.pending_count == 4
    finally:
        for gate in release:
            gate.set()
        driver.join(timeout=5)

    evaluation.finish(state)
    assert max_running == 2
    assert evaluation.pending_count == 0
    assert len(evaluation.outcomes) == 6


def test_a_fetch_failure_cancels_queued_reviews_and_stops_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fetching a later page fails, `review_node` cancels queued reviews
    and joins its pool before propagating the error."""
    monkeypatch.setattr(review, "REVIEW_CONCURRENCY", 1)
    s0_started = threading.Event()
    s0_release = threading.Event()
    reviewed: list[str] = []
    worker_threads: list[threading.Thread] = []

    class _ShutdownHookPool(futures.ThreadPoolExecutor):
        def shutdown(
            self, wait: bool = True, *, cancel_futures: bool = False
        ) -> None:
            worker_threads.extend(self._threads)
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            s0_release.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(review.futures, "ThreadPoolExecutor", _ShutdownHookPool)

    def review_with_blocked_first_case(
        eval_case: Any, *args: Any, **kwargs: Any
    ) -> list[Any]:
        reviewed.append(eval_case.eval_case_id)
        if eval_case.eval_case_id == "s0":
            s0_started.set()
            assert s0_release.wait(timeout=5)
        return []

    class _FailingFetcher:
        has_selector = False
        counts = IngestionCounts(scanned=4, ingested=2, partial=0, failed=0)

        def count_scanned(self, *args: Any, **kwargs: Any) -> int:
            return 4

        def submit_query(self, **kwargs: Any) -> str:
            return "exec-1"

        def fetch_page(self, **kwargs: Any) -> Page:
            if kwargs.get("page_token") is None:
                return make_page(
                    [make_agent_case("s0"), make_agent_case("s1")],
                    next_page_token="1",
                )
            assert s0_started.wait(timeout=5)
            raise RuntimeError("bigquery failed")

    now = dt.datetime.now(tz=dt.UTC)
    ctx = mock.MagicMock()
    ctx.state = {
        "observed_agent_name": "watched",
        "project_id": "p",
        "budget_per_metric": 100,
        "window_start": (now - dt.timedelta(days=1)).isoformat(),
        "window_end": now.isoformat(),
    }
    monkeypatch.setattr(
        _common, "fetcher_factory", lambda _ctx: _FailingFetcher()
    )
    monkeypatch.setattr(
        _common, "goal_loader", lambda _ctx: InvestigationGoal()
    )
    monkeypatch.setattr(
        _common,
        "session_reviewer_factory",
        lambda _ctx: review_with_blocked_first_case,
    )

    with pytest.raises(RuntimeError, match="bigquery failed"):
        review.review_node._func(ctx)

    assert reviewed == ["s0"]
    assert worker_threads
    assert all(not thread.is_alive() for thread in worker_threads)


def test_subsequent_review_errors_do_not_retain_their_exception_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the earliest infrastructure error is kept for the outage rule;
    later errors are dropped on collection so attributes such as HTTP request
    bodies do not accumulate across pages."""
    # Pytest's root log handler retains every logged `exc_info` tuple for the
    # duration of the test, which would keep every raised error alive itself.
    monkeypatch.setattr(review.logger, "propagate", False)

    class _Payload:
        pass

    class _PayloadError(RuntimeError):
        def __init__(self, payload: _Payload) -> None:
            super().__init__("rate limited")
            self.payload = payload

    first_payload = _Payload()
    second_payload = _Payload()
    first_ref = weakref.ref(first_payload)
    second_ref = weakref.ref(second_payload)
    payloads = {"s1": first_payload, "s2": second_payload}
    del first_payload, second_payload

    def fail_with_payload(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
        payload = payloads.pop(eval_case.eval_case_id, None)
        if payload is not None:
            raise _PayloadError(payload)
        return []

    evaluation = review.ReviewEvaluation(fail_with_payload, InvestigationGoal())
    state: dict[str, Any] = {}
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))
    evaluation.evaluate(
        state, make_page([make_agent_case("s2"), make_agent_case("s3")])
    )
    evaluation.finish(state)
    gc.collect()

    assert first_ref() is not None
    assert second_ref() is None
    assert evaluation.outcomes == {
        "s1": "errored",
        "s2": "errored",
        "s3": "passed",
    }


def test_reviews_finishing_between_pages_are_collected_below_the_window() -> (
    None
):
    """Reviews that finish while the next page is fetched are collected and
    have their ready pages recorded even when total sessions stay below the
    `2 * REVIEW_CONCURRENCY` window."""
    gates = {"s0": threading.Event(), "s1": threading.Event()}
    started = {"s0": threading.Event(), "s1": threading.Event()}

    def review_one(eval_case: Any, *args: Any, **kwargs: Any) -> list[Finding]:
        case_id = eval_case.eval_case_id
        started[case_id].set()
        assert gates[case_id].wait(timeout=5)
        return [
            Finding(
                expected_behavior="Book the flight.",
                actual_behavior=f"Missed {case_id}.",
                trajectory_id=case_id,
            )
        ]

    evaluation = review.ReviewEvaluation(review_one, InvestigationGoal())
    state: dict[str, Any] = {}
    try:
        evaluation.evaluate(state, make_page([make_agent_case("s0")]))
        assert started["s0"].wait(timeout=5)
        assert evaluation.pending_count == 1

        # Complete page 0's review between `evaluate` calls, as happens while
        # `fetch_page` reads the next page from BigQuery.
        (s0_future,) = list(evaluation._in_flight)
        gates["s0"].set()
        futures.wait([s0_future], timeout=5)
        assert s0_future.done()

        evaluation.evaluate(state, make_page([make_agent_case("s1")]))
        assert started["s1"].wait(timeout=5)

        assert evaluation.pending_count == 1
        assert evaluation.findings_count == 1
        assert [
            [f["trajectory_id"] for f in fs["findings"]]
            for fs in state["finding_sets"]
        ] == [["s0"], []]
    finally:
        for gate in gates.values():
            gate.set()
        evaluation.finish(state)

    assert [
        [f["trajectory_id"] for f in fs["findings"]]
        for fs in state["finding_sets"]
    ] == [["s0"], ["s1"]]
