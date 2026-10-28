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

"""Tests for running code metrics and reducing failures to findings."""

from __future__ import annotations

from typing import Any

import pytest
from agentplatform._genai.types import (
    EvalCase,
    EvalCaseMetricResult,
    ResponseCandidate,
    Rubric,
    RubricContent,
    RubricContentProperty,
    RubricVerdict,
)
from agentplatform._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.tools.ingestion.models import Page
from ambient_quality_agent.tools.metrics import runner
from ambient_quality_agent.tools.metrics.library import CodeMetric
from google.genai import types as gt

from .conftest import make_code_metric, make_page

EXPECTED = "The agent answers."


_metric = make_code_metric


def _event(
    text: str | None, *, author: str = "agent", thought: bool = False
) -> AgentEvent:
    parts = (
        [] if text is None else [gt.Part(text=text, thought=thought or None)]
    )
    role = "user" if author == "user" else "model"
    return AgentEvent(author=author, content=gt.Content(role=role, parts=parts))


def _case(
    case_id: str = "s1", turns: list[ConversationTurn] | None = None
) -> EvalCase:
    """Builds an EvalCase structured with `agent_data` only.

    Args:
        case_id: Evaluated case identifier.
        turns: Optional list of conversation turns.

    Returns:
        Configured EvalCase instance.
    """
    return EvalCase(
        eval_case_id=case_id,
        agent_data=AgentData(
            agents={},
            turns=turns
            if turns is not None
            else [
                ConversationTurn(
                    turn_index=0,
                    turn_id="t0",
                    events=[_event("Hi?", author="user"), _event("Hello.")],
                )
            ],
        ),
    )


def _page(
    cases: list[EvalCase], revisions: dict[str, str] | None = None
) -> Page:
    return make_page(cases, revisions=revisions or {})


def _score(
    page: Page, library: dict[str, CodeMetric]
) -> tuple[Any, runner.Tally]:
    tally = runner.Tally()
    return runner.score_page(page, library, tally), tally


# --- build_instance -------------------------------------------------------


def test_the_response_is_derived_from_the_conversation() -> None:
    """No fetcher in this repo sets `EvalCase.responses`, so a literal reading
    of the SDK's shaping would score every real session as having said
    nothing."""
    instance = runner.build_instance(_case())

    assert instance["response"]["parts"][0]["text"] == "Hello."


def test_the_response_comes_from_the_last_turn_the_agent_spoke_in() -> None:
    turns = [
        ConversationTurn(turn_index=0, turn_id="t0", events=[_event("First.")]),
        ConversationTurn(turn_index=1, turn_id="t1", events=[_event("Last.")]),
    ]

    instance = runner.build_instance(_case(turns=turns))

    assert instance["response"]["parts"][0]["text"] == "Last."


def test_thought_parts_are_not_the_agent_s_answer() -> None:
    # Both parts on ONE event: a thought in an earlier event would be skipped
    # by the reverse walk anyway, so it would not exercise the filter at all.
    mixed = AgentEvent(
        author="agent",
        content=gt.Content(
            role="model",
            parts=[
                gt.Part(text="Let me think.", thought=True),
                gt.Part(text="Done."),
            ],
        ),
    )
    turn = ConversationTurn(turn_index=0, turn_id="t0", events=[mixed])

    instance = runner.build_instance(_case(turns=[turn]))

    assert instance["response"]["parts"][0]["text"] == "Done."


def test_an_event_that_is_only_a_thought_is_not_an_answer() -> None:
    only_thought = AgentEvent(
        author="agent",
        content=gt.Content(
            role="model", parts=[gt.Part(text="Hmm.", thought=True)]
        ),
    )
    turn = ConversationTurn(turn_index=0, turn_id="t0", events=[only_thought])

    assert runner.build_instance(_case(turns=[turn]))["response"] == {}


def test_a_turn_where_the_agent_said_nothing_yields_an_empty_response() -> None:
    turn = ConversationTurn(turn_index=0, turn_id="t0", events=[_event(None)])

    assert runner.build_instance(_case(turns=[turn]))["response"] == {}


def _user(text: str) -> AgentEvent:
    return _event(text, author="user")


def _call() -> AgentEvent:
    """An agent tool call: agent activity with no text."""
    return AgentEvent(
        author="agent",
        content=gt.Content(
            role="model",
            parts=[
                gt.Part(function_call=gt.FunctionCall(name="weather", args={}))
            ],
        ),
    )


def _result() -> AgentEvent:
    """A tool result, which ADK records as a `user`-role event with no text."""
    return AgentEvent(
        author="agent",
        content=gt.Content(
            role="user",
            parts=[
                gt.Part(
                    function_response=gt.FunctionResponse(
                        name="weather", response={}
                    )
                )
            ],
        ),
    )


# The session shapes `{prompt}` and `{response}` are derived for, as agents-cli
# derives them for a case. The response is the agent's final text after the
# user's last message, empty when it said nothing. The prompt is set only for a
# single-turn session -- one that opens with its only user message -- because
# agents-cli sets `EvalCase.prompt` only on single-turn cases.
_EXCHANGES = [
    (
        "one question, one answer",
        [_user("Weather in Chicago?"), _event("It is 90F.")],
        "Weather in Chicago?",
        "It is 90F.",
    ),
    (
        "the last of several exchanges",
        [
            _user("Rome?"),
            _event("Sunny."),
            _user("And Paris?"),
            _event("Rainy."),
        ],
        None,
        "Rainy.",
    ),
    (
        "an answer behind a tool round trip",
        [_user("Seattle?"), _call(), _result(), _event("55F and raining.")],
        "Seattle?",
        "55F and raining.",
    ),
    (
        "a last message the agent never answered",
        [_user("Rome?"), _event("Sunny."), _user("Thanks!")],
        None,
        "",
    ),
    (
        "a question the agent never answered",
        [_user("Oslo?"), _event(None)],
        "Oslo?",
        "",
    ),
    (
        "a question the agent started on but never answered",
        [_user("Rome?"), _event("Sunny."), _user("And Paris?"), _call()],
        None,
        "",
    ),
    (
        "a session the user never spoke in",
        [_event("Hi! Ask me about the weather.")],
        None,
        "Hi! Ask me about the weather.",
    ),
    (
        "consecutive user messages",
        [
            _user("Weather in Rome?"),
            _user("In Celsius please."),
            _event("24C, sunny."),
        ],
        None,
        "24C, sunny.",
    ),
    (
        "the final text after an interim one",
        [
            _user("And Paris?"),
            _event("Let me check."),
            _call(),
            _event("Rainy."),
        ],
        "And Paris?",
        "Rainy.",
    ),
    (
        "a user message after an agent greeting",
        [_event("Hi!"), _user("Rome?"), _event("Sunny.")],
        None,
        "Sunny.",
    ),
]


@pytest.mark.parametrize(
    ("events", "prompt", "response"),
    [pytest.param(e, p, r, id=label) for label, e, p, r in _EXCHANGES],
)
def test_the_prompt_and_response_are_what_agents_cli_builds_for_a_case(
    events: list[AgentEvent], prompt: str | None, response: str
) -> None:
    """No fetcher sets `EvalCase.prompt` or `responses`, so both are derived."""
    turn = ConversationTurn(turn_index=0, turn_id="t0", events=events)

    instance = runner.build_instance(_case(turns=[turn]))

    assert instance.get("prompt") == (
        {"role": "user", "parts": [{"text": prompt}]}
        if prompt is not None
        else None
    )
    assert instance["response"] == (
        {"role": "model", "parts": [{"text": response}]} if response else {}
    )


def test_the_last_exchange_spans_turns() -> None:
    turns = [
        ConversationTurn(turn_index=0, turn_id="t0", events=[_user("Rome?")]),
        ConversationTurn(turn_index=1, turn_id="t1", events=[_event("Sunny.")]),
    ]

    instance = runner.build_instance(_case(turns=turns))

    assert instance["prompt"]["parts"][0]["text"] == "Rome?"
    assert instance["response"]["parts"][0]["text"] == "Sunny."


def test_a_tool_response_is_not_the_prompt() -> None:
    """ADK records a tool's result as a `user`-role event with no text."""
    tool_result = AgentEvent(
        author="agent",
        content=gt.Content(
            role="user",
            parts=[
                gt.Part(
                    function_response=gt.FunctionResponse(
                        name="w", response={"t": 1}
                    )
                )
            ],
        ),
    )
    turn = ConversationTurn(
        turn_index=0,
        turn_id="t0",
        events=[
            _event("Weather?", author="user"),
            tool_result,
            _event("Sunny."),
        ],
    )

    prompt = runner.build_instance(_case(turns=[turn]))["prompt"]

    assert prompt["parts"][0]["text"] == "Weather?"


def test_a_silent_session_s_prompt_is_the_last_thing_the_user_said() -> None:
    turn = ConversationTurn(
        turn_index=0,
        turn_id="t0",
        events=[_event("Hi?", author="user"), _event(None)],
    )

    assert runner.build_instance(_case(turns=[turn]))["prompt"]["parts"][0][
        "text"
    ] == ("Hi?")


def test_a_session_the_user_never_spoke_in_has_no_prompt() -> None:
    turn = ConversationTurn(
        turn_index=0, turn_id="t0", events=[_event("Hello.")]
    )

    assert "prompt" not in runner.build_instance(_case(turns=[turn]))


def test_a_conversation_with_no_agent_turn_yields_an_empty_response() -> None:
    turn = ConversationTurn(
        turn_index=0, turn_id="t0", events=[_event("Hi?", author="user")]
    )

    assert runner.build_instance(_case(turns=[turn]))["response"] == {}


def test_an_explicit_response_candidate_wins() -> None:
    base = _case()
    case = EvalCase(
        eval_case_id=base.eval_case_id,
        agent_data=base.agent_data,
        responses=[
            ResponseCandidate(
                response=gt.Content(
                    role="model", parts=[gt.Part(text="Explicit.")]
                )
            )
        ],
    )

    assert (
        runner.build_instance(case)["response"]["parts"][0]["text"]
        == "Explicit."
    )


def test_the_prompt_is_carried_when_the_case_has_one() -> None:
    """It wins over the one derived from the conversation, as in the SDK."""
    base = _case()
    case = EvalCase(
        eval_case_id=base.eval_case_id,
        agent_data=base.agent_data,
        prompt=gt.Content(role="user", parts=[gt.Part(text="Given.")]),
    )

    assert runner.build_instance(case)["prompt"]["parts"][0]["text"] == "Given."


def test_the_instance_never_carries_the_responses_field() -> None:
    assert "responses" not in runner.build_instance(_case())


# --- the return contract --------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        {"score": 1.0},
        {"score": 1.0, "explanation": "ignored"},
        1.0,
        1,
        2.0,
        True,
    ],
)
def test_a_passing_outcome_yields_no_finding(outcome: Any) -> None:
    finding_set, tally = _score(_page([_case()]), {"m": _metric(outcome)})

    assert finding_set.findings == []
    assert (tally.passed, tally.failed, tally.errored) == (1, 0, 0)


def test_a_failing_outcome_becomes_a_finding() -> None:
    finding_set, tally = _score(
        _page([_case()]),
        {"m": _metric({"score": 0.0, "explanation": "create_ticket raised."})},
    )

    assert len(finding_set.findings) == 1
    finding = finding_set.findings[0]
    assert finding.expected_behavior == EXPECTED
    assert finding.actual_behavior == "create_ticket raised."
    assert finding.session_id == "s1"
    assert (tally.failed, tally.findings) == (1, 1)


def test_false_is_a_failing_score_as_it_is_to_the_sdk() -> None:
    # A metric ending `return all(checks)` must behave the same here and under
    # `agents-cli eval run`.
    finding_set, tally = _score(_page([_case()]), {"m": _metric(False)})

    assert len(finding_set.findings) == 1
    assert tally.errored == 0


def test_a_partial_score_is_a_failure() -> None:
    # A metric 40% satisfied found something; rounding it to a pass loses it.
    finding_set, _ = _score(
        _page([_case()]),
        {"m": _metric({"score": 0.4, "explanation": "partly"})},
    )

    assert len(finding_set.findings) == 1


def test_a_failure_without_an_explanation_still_reaches_clustering() -> None:
    finding_set, _ = _score(_page([_case()]), {"m": _metric({"score": 0.0})})

    assert len(finding_set.findings) == 1
    assert finding_set.findings[0].actual_behavior.strip()


@pytest.mark.parametrize(
    "outcome",
    [
        "not a number",
        {"explanation": "no score"},
        None,
        {"score": "high"},
        {"score": float("nan")},
        {"score": float("inf")},
    ],
)
def test_an_off_contract_return_is_an_error_not_a_pass(outcome: Any) -> None:
    finding_set, tally = _score(_page([_case()]), {"m": _metric(outcome)})

    assert finding_set.findings == []
    assert (tally.passed, tally.failed, tally.errored) == (0, 0, 1)
    assert tally.first_error is not None


def test_a_raising_metric_errors_its_case_and_is_remembered() -> None:
    boom = RuntimeError("boom")

    _, tally = _score(_page([_case()]), {"m": _metric(boom)})

    assert tally.errored == 1
    assert tally.first_error is boom


# --- one broken metric must not discard the others ------------------------


def test_a_broken_metric_errors_the_case_but_keeps_the_others_findings() -> (
    None
):
    """Bucketing the case and keeping its findings are independent. The bucket
    is errored, as `InvestigationCounters.traces_eval_errored` defines it and
    as alerting reads it; the working metric's finding still reaches
    clustering."""
    library = {
        "bad": _metric(RuntimeError("boom"), name="bad"),
        "good": _metric(
            {"score": 0.0, "explanation": "still found it"}, name="good"
        ),
    }

    finding_set, tally = _score(_page([_case()]), library)

    assert [f.actual_behavior for f in finding_set.findings] == [
        "still found it"
    ]
    assert (tally.passed, tally.failed, tally.errored) == (0, 0, 1)
    # ...and the working metric is not mistaken for a dead one.
    assert tally.scored_metrics == {"good"}


def test_a_case_every_metric_broke_on_is_errored() -> None:
    library = {
        "bad": _metric(RuntimeError("boom"), name="bad"),
        "worse": _metric("nonsense", name="worse"),
    }

    _, tally = _score(_page([_case()]), library)

    assert (tally.passed, tally.failed, tally.errored) == (0, 0, 1)


def test_a_passing_metric_beside_a_broken_one_is_still_errored() -> None:
    """Case status is errored-wins and this must agree with it, or the
    two producers put different meanings in one counter."""
    library = {
        "bad": _metric(RuntimeError("boom"), name="bad"),
        "good": _metric({"score": 1.0}, name="good"),
    }

    _, tally = _score(_page([_case()]), library)

    assert (tally.passed, tally.errored) == (0, 1)


def test_a_session_that_cannot_be_shaped_is_errored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passing = {"m": _metric({"score": 1.0})}
    _, control = _score(_page([_case()]), passing)
    assert control.errored == 0, "control: this session shapes fine"

    def refuse(eval_case: Any) -> dict[str, Any]:
        raise ValueError("cannot serialize this session")

    monkeypatch.setattr(runner, "build_instance", refuse)
    tally = runner.Tally()
    runner.score_page(_page([_case()]), passing, tally)

    assert (tally.passed, tally.failed, tally.errored) == (0, 0, 1)


def test_a_metric_cannot_change_what_the_next_one_sees() -> None:
    """A bucket metric normalizing what it was handed would otherwise decide
    every later metric's verdict on that session."""
    seen: list[dict] = []

    def vandal(instance: dict[str, Any]) -> dict[str, Any]:
        instance["response"] = {}
        instance["agent_data"] = {}
        return {"score": 1.0}

    def witness(instance: dict[str, Any]) -> dict[str, Any]:
        seen.append(instance)
        return {"score": 1.0}

    library = {
        "vandal": CodeMetric("vandal", EXPECTED, vandal),
        "witness": CodeMetric("witness", EXPECTED, witness),
    }
    _score(_page([_case()]), library)

    assert seen[0]["response"]["parts"][0]["text"] == "Hello."
    assert seen[0]["agent_data"]


# --- broken metrics -------------------------------------------------------


def test_a_metric_that_never_worked_is_named() -> None:
    """It leaves every case `scored` by the metrics beside it, so without this
    the sweep reports cleanly on a library half of which never ran."""
    library = {
        "bad": _metric(RuntimeError("boom"), name="bad"),
        "good": _metric({"score": 1.0}, name="good"),
    }
    tally = runner.Tally()
    runner.score_page(_page([_case("s1"), _case("s2")]), library, tally)

    assert tally.scored_metrics == {"good"}


def test_a_metric_that_broke_only_sometimes_still_counts_as_working() -> None:
    calls = {"n": 0}

    def flaky(instance: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")
        return {"score": 1.0}

    tally = runner.Tally()
    runner.score_page(
        _page([_case("s1"), _case("s2")]),
        {"flaky": CodeMetric("flaky", EXPECTED, flaky)},
        tally,
    )

    assert tally.scored_metrics == {"flaky"}


def test_a_metric_that_exits_the_process_is_caught() -> None:
    """Metric modules are written to run as scripts too, so `sys.exit()` in one
    is a mistake, not an attack -- and SystemExit is not an Exception."""
    _, tally = _score(_page([_case()]), {"m": _metric(SystemExit(2))})

    assert tally.errored == 1
    assert tally.scored_metrics == set()


# --- the finding set ------------------------------------------------------


def test_the_revision_lands_on_the_finding() -> None:
    finding_set, _ = _score(
        _page([_case()], revisions={"s1": "rev-7"}),
        {"m": _metric({"score": 0.0, "explanation": "nope"})},
    )

    assert finding_set.findings[0].agent_revision == "rev-7"


def test_a_failing_session_keeps_its_trace_as_evidence() -> None:
    finding_set, _ = _score(
        _page([_case()]), {"m": _metric({"score": 0.0, "explanation": "nope"})}
    )

    assert list(finding_set.sessions) == ["s1"]
    assert finding_set.sessions["s1"], (
        "the turns should be serialized, not empty"
    )


def test_a_passing_session_keeps_no_trace() -> None:
    finding_set, _ = _score(_page([_case()]), {"m": _metric({"score": 1.0})})

    assert finding_set.sessions == {}


def test_one_session_failing_twice_is_stored_once() -> None:
    library = {
        "a": _metric({"score": 0.0, "explanation": "one"}, name="a"),
        "b": _metric({"score": 0.0, "explanation": "two"}, name="b"),
    }

    finding_set, _ = _score(_page([_case()]), library)

    assert len(finding_set.findings) == 2
    assert list(finding_set.sessions) == ["s1"]


def test_the_tally_accumulates_across_pages() -> None:
    tally = runner.Tally()
    library = {"m": _metric({"score": 0.0, "explanation": "nope"})}
    runner.score_page(_page([_case("s1")]), library, tally)
    runner.score_page(_page([_case("s2")]), library, tally)

    assert (tally.cases, tally.failed, tally.findings) == (2, 2, 2)


def test_an_empty_page_changes_nothing() -> None:
    finding_set, tally = _score(_page([]), {"m": _metric({"score": 1.0})})

    assert finding_set.findings == []
    assert tally.cases == 0


# --- the SDK's own result shape, the third `agents-cli` accepts -------------


def _sdk_result(
    score: float, reasoning: str | None = None
) -> EvalCaseMetricResult:
    verdicts = (
        [
            RubricVerdict(
                evaluated_rubric=Rubric(
                    rubric_id="r",
                    content=RubricContent(
                        property=RubricContentProperty(description="x")
                    ),
                ),
                verdict=False,
                reasoning=reasoning,
            )
        ]
        if reasoning is not None
        else None
    )
    return EvalCaseMetricResult(
        metric_name="m", score=score, rubric_verdicts=verdicts
    )


def test_an_eval_case_metric_result_with_a_failed_verdict_becomes_a_finding() -> (
    None
):
    finding_set, tally = _score(
        _page([_case()]),
        {"m": _metric(_sdk_result(0.0, "the ticket lost its urgency"))},
    )

    assert [f.actual_behavior for f in finding_set.findings] == [
        "the ticket lost its urgency"
    ]
    assert tally.errored == 0


def test_an_eval_case_metric_result_that_passed_yields_no_finding() -> None:
    finding_set, tally = _score(
        _page([_case()]), {"m": _metric(_sdk_result(1.0))}
    )

    assert finding_set.findings == []
    assert (tally.passed, tally.errored) == (1, 0)


def test_an_eval_case_metric_result_scored_low_without_verdicts_still_fails() -> (
    None
):
    result = EvalCaseMetricResult(
        metric_name="m", score=0.0, explanation="too slow"
    )

    finding_set, _ = _score(_page([_case()]), {"m": _metric(result)})

    assert [f.actual_behavior for f in finding_set.findings] == ["too slow"]


@pytest.mark.parametrize(
    "result",
    [
        EvalCaseMetricResult(
            metric_name="m", error_message="CustomFunctionError(m): boom"
        ),
        EvalCaseMetricResult(metric_name="m"),
        EvalCaseMetricResult(
            metric_name="m", score=None, explanation="no score"
        ),
    ],
)
def test_an_errored_eval_case_metric_result_is_not_a_pass(
    result: EvalCaseMetricResult,
) -> None:
    """The SDK builds exactly this shape when a metric raises. Reading it as a
    pass would let a metric failing on every session report a clean sweep."""
    finding_set, tally = _score(_page([_case()]), {"m": _metric(result)})

    assert finding_set.findings == []
    assert (tally.passed, tally.errored) == (0, 1)


def test_a_result_whose_verdicts_all_passed_is_a_pass() -> None:
    verdict = RubricVerdict(
        evaluated_rubric=Rubric(
            rubric_id="r",
            content=RubricContent(
                property=RubricContentProperty(description="x")
            ),
        ),
        verdict=True,
        reasoning="fine",
    )
    result = EvalCaseMetricResult(
        metric_name="m", score=None, rubric_verdicts=[verdict]
    )

    _, tally = _score(_page([_case()]), {"m": _metric(result)})

    assert (tally.passed, tally.errored) == (1, 0)


# --- results that decided nothing, and answers hidden behind a tool call -----


def test_verdicts_that_all_decided_nothing_are_errored_not_passed() -> None:
    """`list_failed_rubric_verdicts` reads a scoreless result as having passed, so
    without this a metric that never ran reports a clean sweep."""
    result = EvalCaseMetricResult(
        metric_name="m",
        rubric_verdicts=[
            RubricVerdict(verdict=None),
            RubricVerdict(verdict=None),
        ],
    )

    with pytest.raises(ValueError, match="decided nothing"):
        runner._extract_failure_text(result)


def test_one_decided_verdict_among_undecided_ones_still_passes() -> None:
    result = EvalCaseMetricResult(
        metric_name="m",
        rubric_verdicts=[
            RubricVerdict(verdict=True),
            RubricVerdict(verdict=None),
        ],
    )

    assert runner._extract_failure_text(result) is None


def test_an_undecided_verdict_behind_a_failing_score_is_a_failure() -> None:
    """A score decides it: `list_failed_rubric_verdicts` counts an undecided verdict
    as failed once the score says the metric did not pass."""
    result = EvalCaseMetricResult(
        metric_name="m",
        score=0.0,
        rubric_verdicts=[RubricVerdict(verdict=None, reasoning="no unit")],
    )

    assert runner._extract_failure_text(result) == "no unit"


def _turn(*events: AgentEvent) -> ConversationTurn:
    return ConversationTurn(events=list(events))


def _said(text: str) -> AgentEvent:
    return AgentEvent(
        content=gt.Content(role="model", parts=[gt.Part(text=text)])
    )


def _tool_call() -> AgentEvent:
    return AgentEvent(
        content=gt.Content(
            role="model",
            parts=[
                gt.Part(function_call=gt.FunctionCall(name="lookup", args={}))
            ],
        )
    )


def test_the_answer_is_found_behind_a_trailing_tool_call() -> None:
    """A session ending in a tool call would otherwise hand every metric an
    empty response and score the agent on silence the user never saw."""
    agent_data = AgentData(
        turns=[_turn(_said("It is 20C.")), _turn(_tool_call())]
    )

    assert runner._extract_final_agent_text(agent_data) == "It is 20C."


def test_the_answer_is_found_behind_a_trailing_thought() -> None:
    thought = AgentEvent(
        content=gt.Content(
            role="model", parts=[gt.Part(text="hmm", thought=True)]
        )
    )
    agent_data = AgentData(turns=[_turn(_said("It is 20C.")), _turn(thought)])

    assert runner._extract_final_agent_text(agent_data) == "It is 20C."


def test_a_session_where_the_agent_never_spoke_is_still_empty() -> None:
    agent_data = AgentData(turns=[_turn(_tool_call())])

    assert runner._extract_final_agent_text(agent_data) == ""
