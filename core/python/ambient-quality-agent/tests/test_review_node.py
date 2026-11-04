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
import functools
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.core.nodes import review
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
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
    evaluation, _ = _review_with(
        _build_failing_reviewer(agentic_reviewer.UnparsableReplyError("prose")),
        [make_agent_case("s1")],
    )

    evaluation.finish({})

    assert evaluation.outcomes == {"s1": "errored"}


def test_every_review_failing_on_infrastructure_fails_the_sweep() -> None:
    evaluation, _ = _review_with(
        _build_failing_reviewer(RuntimeError("model unreachable")),
        [make_agent_case("s1"), make_agent_case("s2")],
    )

    with pytest.raises(RuntimeError, match="model unreachable"):
        evaluation.finish({})


def test_unparsable_replies_do_not_hide_an_infrastructure_outage() -> None:
    def fail(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
        if eval_case.eval_case_id == "s1":
            raise agentic_reviewer.UnparsableReplyError("prose")
        raise RuntimeError("model unreachable")

    evaluation, _ = _review_with(
        fail, [make_agent_case("s1"), make_agent_case("s2")]
    )

    with pytest.raises(RuntimeError, match="model unreachable"):
        evaluation.finish({})


def test_one_infrastructure_success_keeps_the_sweep() -> None:
    def fail_once(eval_case: Any, *args: Any, **kwargs: Any) -> Any:
        if eval_case.eval_case_id == "s1":
            raise RuntimeError("model unreachable")
        return []

    evaluation, _ = _review_with(
        fail_once, [make_agent_case("s1"), make_agent_case("s2")]
    )

    evaluation.finish({})

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


def test_each_review_sees_the_callers_context_variables() -> None:
    seen: list[str] = []

    def record_context(*args: Any, **kwargs: Any) -> Any:
        seen.append(_CALLER_VALUE.get())
        return []

    token = _CALLER_VALUE.set("node span")
    try:
        _review_with(
            record_context, [make_agent_case("s1"), make_agent_case("s2")]
        )
    finally:
        _CALLER_VALUE.reset(token)

    assert seen == ["node span", "node span"]
