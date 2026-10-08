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

"""Tests for code metrics the eval service executes rather than this process."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from agentplatform._genai.types import (
    CodeExecutionMetric,
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationResult,
    ResponseCandidate,
    ResponseCandidateResult,
)
from agentplatform._genai.types.evals import AgentEvent
from ambient_quality_agent.core.nodes._remote_code_metrics import (
    RemoteCodeMetricEvaluation,
)
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    PASSED,
)
from ambient_quality_agent.tools.metrics import remote, runner
from ambient_quality_agent.tools.metrics.library import (
    RemoteCodeMetric,
    load_library,
)
from google.genai import types as gt

from .conftest import make_agent_case, make_page

_SOURCE = "EXPECTED = 'The agent answers.'\n\ndef evaluate(instance):\n    return 1.0\n"


def _config(
    *entries: dict[str, Any], run: list[str] | None = None
) -> dict[str, bytes]:
    """Builds an eval configuration mapping publishing `entries`.

    Args:
        *entries: Metric configuration dictionaries.
        run: Optional list of metric names to run.

    Returns:
        Mapping of file paths to serialized config file contents.
    """
    import json

    names = run if run is not None else [e["name"] for e in entries]
    body = {"metrics_to_run": names, "custom_metrics": list(entries)}
    return {"eval_config.json": json.dumps(body).encode()}


def _instance_for(eval_case: Any) -> dict[str, Any]:
    """Builds the instance the SDK sends for `eval_case` using the SDK handler.

    Driven through the real handler rather than asserted against a copy of its
    shape, so changes in how the service is fed fail here.

    Args:
        eval_case: Evaluation case to build the request payload for.

    Returns:
        Dictionary payload representing the instance.
    """
    from agentplatform._genai import _evals_metric_handlers as handlers

    handler = handlers.CustomCodeExecutionMetricHandler.__new__(
        handlers.CustomCodeExecutionMetricHandler
    )
    handler.metric = CodeExecutionMetric(name="m", custom_function=_SOURCE)
    payload = handler._build_request_payload(eval_case, 0)
    return payload["instance"].model_dump(exclude_none=True)


def _result(*metrics: dict[str, EvalCaseMetricResult]) -> EvaluationResult:
    """Builds a page of evaluation results with one case per provided metric mapping.

    Metric names are lowercased because the service answers under lowercased
    names: the SDK lowercases `Metric.name` when the request is built. Lowercasing
    here prevents tests from masking case-sensitivity mismatches.

    Args:
        *metrics: Mappings from metric names to evaluation case metric results.

    Returns:
        Constructed evaluation result containing the mapped cases.
    """
    return EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=index,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={n.lower(): r for n, r in named.items()},
                    )
                ],
            )
            for index, named in enumerate(metrics)
        ]
    )


# --- loading ----------------------------------------------------------------- #


def test_a_remote_custom_function_is_loaded_instead_of_skipped() -> None:
    """Remote custom functions are loaded into the library as runnable metrics."""
    library = load_library(
        _config({"name": "remote_one", "remote_custom_function": _SOURCE})
    )

    assert not library, (
        "a remote metric must not be compiled into the local mapping"
    )
    assert [m.name for m in library.remote] == ["remote_one"]
    assert library.remote[0].source == _SOURCE


def test_execution_remote_sends_the_local_function_to_the_service() -> None:
    """The other marker: an ordinary `custom_function` asked to run elsewhere."""
    library = load_library(
        _config(
            {
                "name": "elsewhere",
                "custom_function": _SOURCE,
                "execution": "remote",
            }
        )
    )

    assert [m.name for m in library.remote] == ["elsewhere"]
    assert library.remote[0].source == _SOURCE


def test_a_remote_function_file_is_read_from_the_published_objects() -> None:
    objects = _config(
        {
            "name": "from_file",
            "custom_function_file": "metric.py",
            "execution": "remote",
        }
    )
    objects["metric.py"] = _SOURCE.encode()

    library = load_library(objects)

    assert library.remote[0].source == _SOURCE


def test_a_remote_metric_is_never_imported() -> None:
    """The point of running it elsewhere: import side effects stay there.

    A local metric with this body would raise on load and cost its own sweep.
    """
    source = "raise RuntimeError('imported')\n\ndef evaluate(instance):\n    return 1.0\n"

    library = load_library(
        _config({"name": "explodes", "remote_custom_function": source})
    )

    assert [m.name for m in library.remote] == ["explodes"]
    assert not library.failures


def test_the_expected_contract_is_read_without_executing_the_module() -> None:
    """Findings cluster on `EXPECTED`, which the local path reads after import."""
    library = load_library(
        _config({"name": "declared", "remote_custom_function": _SOURCE})
    )

    assert library.remote[0].expected == "The agent answers."


def test_the_loader_resolves_the_name_the_service_will_answer_under() -> None:
    """The join key, taken from the SDK rather than re-derived.

    The SDK lowercases `Metric.name` and answers under the lowered form. Nothing
    else in the tests reads this off the loader, so without it the loader and
    the test helper could drift and the join would break in production only.
    """
    library = load_library(
        _config({"name": "ToolUse_Correct", "remote_custom_function": _SOURCE})
    )

    assert library.remote[0].name == "ToolUse_Correct"
    assert library.remote[0].service_name == "tooluse_correct"


def test_an_annotated_expected_is_read_like_a_plain_one() -> None:
    """`EXPECTED: str = "..."` is an ordinary way to write a typed constant.

    It is a different AST node, and missing it would silently label every
    finding from the metric with the generated default instead.
    """
    source = 'EXPECTED: str = "The agent answers."\n\ndef evaluate(instance):\n    return 1.0\n'
    library = load_library(
        _config({"name": "annotated", "remote_custom_function": source})
    )

    assert library.remote[0].expected == "The agent answers."


def test_the_last_expected_wins_as_it_does_after_an_import() -> None:
    """`getattr` on an imported module sees the last assignment.

    Taking the first would give one source two contracts depending on which
    side ran it, and `expected` is the key findings cluster on -- so the same
    defect would split into two insights.
    """
    source = 'EXPECTED = "first"\nEXPECTED = "second"\n\ndef evaluate(instance):\n    return 1.0\n'

    library = load_library(
        _config({"name": "twice", "remote_custom_function": source})
    )

    namespace: dict[str, Any] = {}
    # Replicates standard module import evaluation to compare with the loader.
    exec(compile(source, "<t>", "exec"), namespace)  # noqa: S102 - executes trusted in-memory test source
    assert library.remote[0].expected == namespace["EXPECTED"] == "second"


def test_a_remote_metric_declaring_no_expected_falls_back_to_its_name() -> None:
    library = load_library(
        _config(
            {
                "name": "bare",
                "remote_custom_function": "def evaluate(instance):\n    return 1.0\n",
            }
        )
    )

    assert (
        library.remote[0].expected == "The agent satisfies the 'bare' metric."
    )


def test_a_remote_metric_without_an_evaluate_function_is_a_named_failure() -> (
    None
):
    """Caught here rather than once per case at the service."""
    library = load_library(
        _config({"name": "empty", "remote_custom_function": "X = 1\n"})
    )

    assert not library.remote
    assert [name for name, _why in library.failures] == ["empty"]


def test_a_remote_metric_that_will_not_parse_is_a_named_failure() -> None:
    library = load_library(
        _config(
            {"name": "broken", "remote_custom_function": "def evaluate(:\n"}
        )
    )

    assert not library.remote
    assert [name for name, _why in library.failures] == ["broken"]


def test_a_remote_metric_naming_no_function_is_named_not_dropped() -> None:
    """`execution: remote` with nothing to run is the shape a typo lands in.

    Skipped quietly it never reached the run summary, so the operator saw a
    sweep that scored nothing and said nothing about why.
    """
    library = load_library(_config({"name": "nothing", "execution": "remote"}))

    assert not library.remote
    assert [name for name, _why in library.failures] == ["nothing"]


@pytest.mark.parametrize("execution", ["local", "remote"])
def test_execution_decides_which_side_runs_the_same_function(
    execution: str,
) -> None:
    """One `custom_function`, and only `execution` says where it runs.

    Refusing the values that are neither is covered in `test_judge_metrics`.
    """
    library = load_library(
        _config(
            {"name": "m", "custom_function": _SOURCE, "execution": execution}
        )
    )

    assert not library.failures
    assert bool(library.remote) == (execution == "remote")
    assert bool(library) == (execution == "local")


@pytest.mark.parametrize(
    "body",
    [
        "    return {'score': 1.0, 'explanation': 'fine'}\n",
        "    return dict(score=1.0, explanation='fine')\n",
        "    if instance:\n        return {'score': 0.0}\n    return 1.0\n",
    ],
)
def test_a_metric_returning_a_score_mapping_is_refused(body: str) -> None:
    """The likeliest mistake when a working local metric is marked remote.

    The service scores that mapping 0.0, so against the default threshold every
    session becomes a defect -- one identical, reason-less finding each, with
    nothing raised and nothing logged. Refused at load instead, with a reason.
    """
    library = load_library(
        _config(
            {
                "name": "mapping",
                "remote_custom_function": f"def evaluate(instance):\n{body}",
            }
        )
    )

    assert not library.remote
    (name, why) = library.failures[0]
    assert name == "mapping"
    assert "must return a number" in why


@pytest.mark.parametrize(
    "body",
    [
        "    return 1.0\n",
        "    if instance:\n        return 0.0\n    return 1.0\n",
        "    return score_of(instance)\n",
    ],
)
def test_a_metric_returning_a_number_is_accepted(body: str) -> None:
    """The guard reads only visible returns, so it must not over-refuse."""
    library = load_library(
        _config(
            {
                "name": "numeric",
                "remote_custom_function": f"def evaluate(instance):\n{body}",
            }
        )
    )

    assert [m.name for m in library.remote] == ["numeric"]
    assert not library.failures


def test_a_remote_metric_carries_its_authors_threshold() -> None:
    library = load_library(
        _config(
            {
                "name": "scaled",
                "remote_custom_function": _SOURCE,
                "threshold": 3.0,
            }
        )
    )

    assert library.remote[0].threshold == pytest.approx(3.0)


def test_local_and_remote_metrics_load_side_by_side() -> None:
    library = load_library(
        _config(
            {"name": "here", "custom_function": _SOURCE},
            {"name": "there", "remote_custom_function": _SOURCE},
        )
    )

    assert list(library) == ["here"]
    assert [m.name for m in library.remote] == ["there"]


# --- the metrics sent to the service ------------------------------------------ #


def test_each_remote_metric_becomes_a_code_execution_metric() -> None:
    """`CodeExecutionMetric.custom_function` is the field the SDK serializes
    into `custom_code_execution_spec`, which is what runs server side."""
    metrics = remote.build_eval_metrics([_metric()])

    assert [type(m) for m in metrics] == [CodeExecutionMetric]
    assert metrics[0].name == "m"
    assert metrics[0].custom_function == _SOURCE


def test_the_sdk_routes_the_metric_to_the_server_side_handler() -> None:
    """The security property itself, asserted on the real dispatcher.

    Holding `custom_function` proves only that pydantic stored it. The handler
    one row below in the SDK's mapping, `CustomMetricHandler`, runs the function
    *in this process* on the agent's credentials -- so the field alone reads the
    same whichever of the two the metric reaches.
    """
    from agentplatform._genai import _evals_metric_handlers as handlers

    metric = remote.build_eval_metrics([_metric()])[0]

    matched = [
        h for cond, h in handlers._METRIC_HANDLER_MAPPING if cond(metric)
    ]

    assert matched[0] is handlers.CustomCodeExecutionMetricHandler


def test_a_metric_named_with_capitals_still_matches_its_result() -> None:
    """The SDK lowercases `Metric.name`, so the service answers about
    `tooluse_correct` however the config spelled it.

    Keyed on the config spelling, every result missed, every session was
    reported a clean pass, and the only trace was a summary line that reads
    exactly like a metric which legitimately found nothing.
    """
    metric = _metric("ToolUse_Correct")
    assert metric.service_name == "tooluse_correct", "the SDK's own lowering"

    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result(
            {
                "ToolUse_Correct": EvalCaseMetricResult(
                    score=0.0, explanation="no"
                )
            }
        ),
        _sent(metric),
        tally,
    )

    assert [f.actual_behavior for f in finding_set.findings] == ["no"]
    assert (tally.failed, tally.passed) == (1, 0)
    assert tally.scored_metrics == {"ToolUse_Correct"}


# --- scoring ------------------------------------------------------------------ #


def _metric(name: str = "m", threshold: float = 1.0) -> RemoteCodeMetric:
    """Creates a remote metric with its service name resolved by the SDK.

    Args:
        name: Name of the remote metric.
        threshold: Score threshold required to pass.

    Returns:
        Constructed remote code metric instance.
    """
    return RemoteCodeMetric(
        name=name,
        service_name=CodeExecutionMetric(
            name=name, custom_function=_SOURCE
        ).name
        or name,
        expected="The agent answers.",
        source=_SOURCE,
        threshold=threshold,
    )


def _sent(*metrics: RemoteCodeMetric) -> dict[str, RemoteCodeMetric]:
    """Builds the metric lookup mapping expected by `score_page_results`.

    Args:
        *metrics: Remote code metrics to include in the lookup.

    Returns:
        Dictionary mapping service names to their corresponding remote code metrics.
    """
    return {m.service_name: m for m in metrics}


def test_a_failing_score_becomes_a_finding_with_the_services_explanation() -> (
    None
):
    page = make_page([make_agent_case("s1")])
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result(
            {"m": EvalCaseMetricResult(score=0.0, explanation="wrong units")}
        ),
        _sent(_metric()),
        tally,
    )

    assert [f.actual_behavior for f in finding_set.findings] == ["wrong units"]
    assert [f.expected_behavior for f in finding_set.findings] == [
        "The agent answers."
    ]
    assert [f.session_id for f in finding_set.findings] == ["s1"]
    assert (tally.cases, tally.failed, tally.passed) == (1, 1, 0)


def test_a_finding_says_why_there_is_no_reason() -> None:
    """The service returns no explanation for ANY code metric, so this is the
    text on every remote finding -- not the rare one. The local path's wording
    blames the metric, which would send a triager to read working code.
    """
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=0.0)}),
        _sent(_metric()),
        tally,
    )

    (actual,) = [f.actual_behavior for f in finding_set.findings]
    assert "eval service does not return an explanation" in actual


def test_a_passing_score_produces_no_finding() -> None:
    page = make_page([make_agent_case("s1")])
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=1.0)}),
        _sent(_metric()),
        tally,
    )

    assert not finding_set.findings
    assert (tally.passed, tally.failed, tally.errored) == (1, 0, 0)


def test_the_authors_threshold_decides_the_verdict_not_a_fixed_one() -> None:
    """A metric scoring 1 to 5 passes at the score its author set."""
    page = make_page([make_agent_case("s1")])

    passing = runner.Tally()
    remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=4.0)}),
        _sent(_metric(threshold=3.0)),
        passing,
    )
    failing = runner.Tally()
    remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=4.0)}),
        _sent(_metric(threshold=5.0)),
        failing,
    )

    assert (passing.passed, passing.failed) == (1, 0)
    assert (failing.passed, failing.failed) == (0, 1)


def test_a_service_error_counts_as_errored_and_never_as_a_clean_pass() -> None:
    page = make_page([make_agent_case("s1")])
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result(
            {"m": EvalCaseMetricResult(error_message="the service fell over")}
        ),
        _sent(_metric()),
        tally,
    )

    assert not finding_set.findings
    assert (tally.errored, tally.passed, tally.failed) == (1, 0, 0)
    assert tally.first_error is not None


def test_an_error_outranks_a_failure_on_the_same_case() -> None:
    """One case is one outcome, ranked as the local runner ranks it."""
    page = make_page([make_agent_case("s1")])
    tally = runner.Tally()

    remote.score_page_results(
        page,
        _result(
            {
                "a": EvalCaseMetricResult(score=0.0, explanation="bad"),
                "b": EvalCaseMetricResult(error_message="boom"),
            }
        ),
        _sent(_metric("a"), _metric("b")),
        tally,
    )

    assert (tally.cases, tally.errored, tally.failed, tally.passed) == (
        1,
        1,
        0,
        0,
    )


def test_a_result_for_an_unsent_metric_is_ignored_and_the_case_is_not_a_pass() -> (
    None
):
    """Nothing here knows what a passing score means for a metric it never sent.

    The case is still left unjudged by everything this evaluation did send, so
    it counts as errored. Counting it passed would report an agent clean on the
    strength of a metric nobody asked for.
    """
    page = make_page([make_agent_case("s1")])
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"stranger": EvalCaseMetricResult(score=0.0, explanation="x")}),
        _sent(_metric()),
        tally,
    )

    assert not finding_set.findings
    assert (tally.passed, tally.errored) == (0, 1)


def test_a_result_about_a_case_this_page_does_not_hold_is_ignored() -> None:
    """The bounds check on the positional join, which nothing else executes.

    `score_page_results` is called outside the evaluation's `try`, and the sweep
    does not wrap `evaluate`, so an `IndexError` here would abort the whole
    review node -- taking the session reviewer down with it.
    """
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()
    stray = EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=5,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={"m": EvalCaseMetricResult(score=1.0)},
                    )
                ],
            )
        ]
    )

    finding_set = remote.score_page_results(
        page, stray, _sent(_metric()), tally
    )

    assert not finding_set.findings
    assert (tally.cases, tally.errored) == (1, 1)


def test_a_failing_case_carries_its_turns_and_revision() -> None:
    page = make_page([make_agent_case("s1")])
    page.revisions["s1"] = "rev-7"
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=0.0, explanation="no")}),
        _sent(_metric()),
        tally,
    )

    assert [f.agent_revision for f in finding_set.findings] == ["rev-7"]
    assert list(finding_set.sessions) == ["s1"]


def test_every_case_of_the_page_is_counted_once() -> None:
    """Two cases, one answered: the sweep measured the window, not the reply.

    Asserted by identity rather than by totals -- a join off by one keeps every
    total intact while attributing each verdict to the wrong session.
    """
    page = make_page([make_agent_case("s1"), make_agent_case("s2")])
    tally = runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=0.0, explanation="no")}),
        _sent(_metric()),
        tally,
    )

    assert tally.cases == 2
    # The unanswered case is errored, not passed: the service said nothing
    # about it, which is not the same as the agent behaving.
    assert tally.outcomes == {"s1": "failed", "s2": "errored"}
    assert [f.session_id for f in finding_set.findings] == ["s1"]


def test_a_service_that_answers_about_nothing_does_not_report_a_clean_sweep() -> (
    None
):
    """The failure mode with no exception to catch.

    An empty result yields no iterations at all, so nothing raises and nothing
    is recorded. Defaulting those cases to passed reported three defective
    sessions as three clean ones.
    """
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(EvaluationResult()), [_metric()]
    )
    state: dict[str, Any] = {}

    evaluation.evaluate(
        state, make_page([make_agent_case("s1"), make_agent_case("s2")])
    )

    assert evaluation.outcomes == {"s1": "errored", "s2": "errored"}
    with pytest.raises(RuntimeError, match="No remote metric"):
        evaluation.finish(state)


def test_id_less_cases_keep_distinct_keys_across_pages() -> None:
    """`build_outcome_keys` numbers id-less cases from how many were seen before.

    Passed a per-page count instead of a running one, page two would reuse
    `#1` and the sweep would collapse two different sessions into one verdict.
    """
    tally = runner.Tally()
    answer = _result({"m": EvalCaseMetricResult(score=1.0)})

    remote.score_page_results(
        page := make_page([make_agent_case("")]),
        answer,
        _sent(_metric()),
        tally,
    )
    remote.score_page_results(page, answer, _sent(_metric()), tally)

    assert sorted(tally.outcomes) == ["#1", "#2"]


def test_a_service_that_answers_about_some_cases_errors_only_the_rest() -> None:
    """Partial silence is the same hazard, and does not even trip the summary."""
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(_result({"m": EvalCaseMetricResult(score=1.0)})), [_metric()]
    )

    evaluation.evaluate(
        {}, make_page([make_agent_case("s1"), make_agent_case("s2")])
    )

    assert evaluation.outcomes == {"s1": "passed", "s2": "errored"}


# --- the evaluation in the sweep ---------------------------------------------- #


class _Evaluator:
    """Stands in for the eval service, recording what it was asked to run."""

    def __init__(self, result: EvaluationResult) -> None:
        self._result = result
        self.calls: list[Any] = []

    def evaluate(self, dataset: Any, metrics: Any) -> EvaluationResult:
        self.calls.append((dataset, metrics))
        return self._result


def _state() -> dict[str, Any]:
    return {}


def test_the_evaluation_sends_the_page_to_the_service_once() -> None:
    """One call per page: the SDK already fans it out to a request per case."""
    evaluator = _Evaluator(_result({"m": EvalCaseMetricResult(score=1.0)}))
    evaluation = RemoteCodeMetricEvaluation(evaluator, [_metric()])
    page = make_page([make_agent_case("s1")])

    evaluation.evaluate(_state(), page)

    assert len(evaluator.calls) == 1
    _dataset, metrics = evaluator.calls[0]
    assert [m.custom_function for m in metrics] == [_SOURCE]


def test_the_service_receives_the_response_the_local_runner_derives() -> None:
    """A remote metric must be given the agent's answer, as a local one is.

    The fetchers build cases from `agent_data` and never fill `responses`, and
    the SDK reads a response only from there. Sent unchanged, the service would
    see `agent_data` alone, and a metric reading the response would find none
    on every session while the same source scored fine locally.
    """
    case = make_agent_case("s1", text="Hello.")
    prepared = remote.build_request_dataset(make_page([case]).dataset)

    sent = prepared.eval_cases[0]
    assert sent.responses, "the derived response never reached the request"
    assert sent.responses[0].response.parts[0].text == "Hello."
    assert (
        runner.build_instance(case)["response"]["parts"][0]["text"] == "Hello."
    )


def _asked(question: str, answer: str) -> Any:
    """A fetcher-shaped case where the user asked `question` first."""
    case = make_agent_case("s1", text=answer)
    user = AgentEvent(
        author="user",
        content=gt.Content(role="user", parts=[gt.Part(text=question)]),
    )
    assert case.agent_data is not None
    (turn,) = case.agent_data.turns or []
    events = [user, *(turn.events or [])]
    case.agent_data.turns = [turn.model_copy(update={"events": events})]
    return case


def test_the_service_receives_the_prompt_the_local_runner_derives() -> None:
    """The SDK renders `{prompt}` only from `EvalCase.prompt`, which no fetcher
    sets. Filled in here the way `runner` derives it for a local metric, so a
    judge's `{prompt}` and a local metric's `instance["prompt"]` agree."""
    case = _asked("Rome?", "Sunny.")

    sent = remote.build_request_dataset(make_page([case]).dataset).eval_cases[0]

    assert sent.prompt is not None, (
        "the derived prompt never reached the request"
    )
    assert sent.prompt.parts[0].text == "Rome?"
    assert runner.build_instance(case)["prompt"]["parts"][0]["text"] == "Rome?"


def test_a_case_that_already_carries_a_response_still_gets_its_prompt() -> None:
    response = ResponseCandidate(
        response=gt.Content(parts=[gt.Part(text="Given.")])
    )
    case = _asked("Rome?", "Sunny.").model_copy(
        update={"responses": [response]}
    )

    sent = remote.build_request_dataset(make_page([case]).dataset).eval_cases[0]

    assert sent.responses == [response]
    assert sent.prompt.parts[0].text == "Rome?"


def test_the_remote_instance_nests_the_response_the_services_own_way() -> None:
    """Pins the request the client builds for a remote metric.

    The client-side wrapping, which is what `_instance_for` can reach offline.
    What the service is finally handed differs again, and is pinned separately
    in `test_the_readme_and_the_docstring_teach_the_same_accessor`.
    """
    prepared = remote.build_request_dataset(
        make_page([make_agent_case("s1", text="Hello.")]).dataset
    )

    instance = _instance_for(prepared.eval_cases[0])

    assert instance["response"]["contents"]["contents"][0]["parts"][0][
        "text"
    ] == ("Hello.")
    # Local metrics read `instance["response"]["parts"]`; remote ones do not.
    assert "parts" not in instance["response"]


_ANSWER_PATH = (
    '["response"]["contents"]["gemini_contents"][0]["parts"][0]["text"]'
)
"""Where a remote metric reads the agent's answer.

Recorded from the live service by raising inside one, which puts the instance
in the error it returns. It cannot be re-derived offline -- `gemini_contents`
appears nowhere in the SDK, whose own client-side dump is
`response.contents.contents`.
"""


def test_the_documented_accessor_matches_the_recorded_shape() -> None:
    """The README teaches this path to metric authors, and `remote`'s docstring
    pins the shape it was read off. A wrong accessor is silent -- every metric
    an author writes reads nothing and scores every session identically -- so
    the two have to stay in step.
    """
    readme = (
        pathlib.Path(__file__).resolve().parents[1]
        / "terraform/modules/aqa/README.md"
    ).read_text()
    docstring = remote.__doc__ or ""

    assert _ANSWER_PATH in readme, "the README must teach the full accessor"
    # The docstring carries the raw shape, so the keys the accessor walks have
    # to be the keys it records.
    for key in ("gemini_contents", "agent_eval_data"):
        assert key in docstring, f"{key} missing from the recorded shape"
        assert key in readme, f"{key} missing from what authors are told"


def test_preparing_the_request_leaves_the_shared_page_untouched() -> None:
    """The sweep hands one page to every evaluation, so this must not mutate it."""
    page = make_page([make_agent_case("s1")])

    remote.build_request_dataset(page.dataset)

    assert not page.dataset.eval_cases[0].responses
    assert page.dataset.eval_cases[0].prompt is None


def test_a_session_that_said_nothing_still_carries_a_response_key() -> None:
    """Sent with no response at all, the SDK omits the key entirely.

    Every metric written against the documented accessor then raises KeyError
    at the service, on exactly the sessions where the agent stayed silent --
    while the local path hands it an empty mapping and scores them.
    """
    page = make_page([make_agent_case("s1", text=None)])

    prepared = remote.build_request_dataset(page.dataset)

    assert prepared.eval_cases[0].responses
    assert prepared.eval_cases[0].responses[0].response.parts[0].text == ""
    assert "response" in _instance_for(prepared.eval_cases[0])


def test_a_session_with_no_user_message_carries_no_prompt() -> None:
    """agents-cli sets `EvalCase.prompt` only on single-turn cases, so a
    `{prompt}` judge cannot render here either."""
    page = make_page(
        [make_agent_case("s1", text="Hi! Ask me about the weather.")]
    )

    prepared = remote.build_request_dataset(page.dataset)

    assert prepared.eval_cases[0].prompt is None


def test_findings_land_in_the_state_key_insight_correlation_reads() -> None:
    evaluator = _Evaluator(
        _result({"m": EvalCaseMetricResult(score=0.0, explanation="wrong")})
    )
    evaluation = RemoteCodeMetricEvaluation(evaluator, [_metric()])
    state = _state()

    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    assert len(state["finding_sets"]) == 1
    assert state["finding_sets"][0]["findings"][0]["actual_behavior"] == "wrong"


def test_the_verdicts_are_keyed_the_way_the_sweep_merges_them() -> None:
    """The sweep counts a case once across analyzers by matching these keys, so
    this path must name a case exactly as the local runner names it."""
    evaluator = _Evaluator(
        _result({"m": EvalCaseMetricResult(score=0.0, explanation="wrong")})
    )
    evaluation = RemoteCodeMetricEvaluation(evaluator, [_metric()])
    state = _state()
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    assert evaluation.outcomes == {"s1": "failed"}
    assert evaluation.findings_count == 1
    assert evaluation.rubrics_count == 0


class _DeadEvaluator:
    """An eval service that is down."""

    def evaluate(self, dataset: Any, metrics: Any) -> EvaluationResult:
        raise RuntimeError("503 eval service unavailable")


def test_a_service_outage_is_recorded_rather_than_raised_mid_sweep() -> None:
    """Uncaught, a 503 leaves `run_sweep` from inside the page loop, before the
    peers have finished their pages or any counter has been written.

    Caught, the sweep completes and `finish` decides the run. This does not make
    the evaluation non-fatal: `finish` still raises when nothing scored.
    """
    evaluation = RemoteCodeMetricEvaluation(_DeadEvaluator(), [_metric()])
    state: dict[str, Any] = {}

    # Records evaluation failures in page outcomes instead of raising.
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    # Errored, not absent and not passed: the session was never judged.
    assert evaluation.outcomes == {"s1": "errored"}
    with pytest.raises(RuntimeError, match="No remote metric"):
        evaluation.finish(state)


def test_an_evaluator_that_cannot_be_built_is_recorded_not_raised() -> None:
    """Building the SDK client resolves credentials and can raise.

    Done in the node, it escaped before any evaluation ran. Deferred, the page
    is recorded errored and `finish` decides the run.
    """

    def factory() -> Any:
        raise RuntimeError("could not resolve credentials")

    evaluation = RemoteCodeMetricEvaluation(factory, [_metric()])
    state: dict[str, Any] = {}

    # Records evaluation failures in page outcomes instead of raising.
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    assert evaluation.outcomes == {"s1": "errored"}
    with pytest.raises(RuntimeError, match="No remote metric"):
        evaluation.finish(state)


def test_a_source_the_sdk_refuses_is_a_named_failure_not_a_dead_node() -> None:
    """The library's AST check and the SDK's substring check disagree.

    `def\\tevaluate` is a function to `ast` and not the substring `def evaluate`
    to the SDK. Built at node construction, that ValidationError escaped before
    any evaluation ran -- one oddly formatted metric costing the whole review.
    """
    library = load_library(
        _config(
            {
                "name": "tabbed",
                "remote_custom_function": "def\tevaluate(instance):\n    return 1.0\n",
            },
            {"name": "fine", "remote_custom_function": _SOURCE},
        )
    )

    assert [m.name for m in library.remote] == ["fine"]
    assert [name for name, _why in library.failures] == ["tabbed"]


def test_every_case_erroring_fails_the_run_rather_than_reporting_nothing() -> (
    None
):
    """An eval-service outage otherwise looks exactly like a clean sweep."""
    evaluator = _Evaluator(
        _result({"m": EvalCaseMetricResult(error_message="down")})
    )
    evaluation = RemoteCodeMetricEvaluation(evaluator, [_metric()])
    state = _state()
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    with pytest.raises(RuntimeError, match="No remote metric"):
        evaluation.finish(state)


def test_one_metric_scoring_keeps_the_sweep_alive_for_the_others() -> None:
    evaluator = _Evaluator(
        _result(
            {
                "a": EvalCaseMetricResult(score=1.0),
                "b": EvalCaseMetricResult(error_message="down"),
            }
        )
    )
    evaluation = RemoteCodeMetricEvaluation(
        evaluator, [_metric("a"), _metric("b")]
    )
    state = _state()
    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    evaluation.finish(state)  # does not raise

    assert "`b`" in evaluation.render_summary()


def test_the_summary_names_metrics_that_never_returned_a_verdict() -> None:
    evaluator = _Evaluator(_result({"a": EvalCaseMetricResult(score=1.0)}))
    evaluation = RemoteCodeMetricEvaluation(
        evaluator, [_metric("a"), _metric("quiet")]
    )
    evaluation.evaluate(_state(), make_page([make_agent_case("s1")]))

    summary = evaluation.render_summary()

    assert "eval service" in summary
    assert "`quiet`" in summary


def test_a_case_one_metric_stayed_silent_about_is_not_a_pass() -> None:
    """Two metrics sent, one answered. The service said nothing about the other.

    Counting that case passed reports the agent clean on evidence only half the
    library produced, which is the same silent-clean-sweep the unanswered-case
    default exists to stop -- just one metric at a time.
    """
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    remote.score_page_results(
        page,
        _result({"a": EvalCaseMetricResult(score=1.0)}),
        _sent(_metric("a"), _metric("b")),
        tally,
    )

    assert tally.outcomes == {"s1": "errored"}
    assert (tally.passed, tally.errored) == (0, 1)


def test_a_case_every_metric_answered_is_still_a_pass() -> None:
    """The guard above must not turn an ordinary clean case into an error."""
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    remote.score_page_results(
        page,
        _result(
            {
                "a": EvalCaseMetricResult(score=1.0),
                "b": EvalCaseMetricResult(score=1.0),
            }
        ),
        _sent(_metric("a"), _metric("b")),
        tally,
    )

    assert tally.outcomes == {"s1": "passed"}


def test_an_expected_overwritten_with_a_blank_falls_back_like_the_local_path() -> (
    None
):
    """`getattr` sees the last assignment, so a blank one wins there too.

    Reading past it to an earlier literal gave the same source two contracts,
    which is the split-insight failure the reverse scan exists to prevent.
    """
    source = 'EXPECTED = "old"\nEXPECTED = ""\n\ndef evaluate(instance):\n    return 1.0\n'

    library = load_library(
        _config({"name": "m", "remote_custom_function": source})
    )

    assert library.remote[0].expected == "The agent satisfies the 'm' metric."


def test_neither_of_two_names_colliding_at_the_service_is_run() -> None:
    """The service names both the same, so its one result is ambiguous.

    Keeping the first would score that result against whichever definition
    happened to load first, under the other's threshold and contract.
    """
    library = load_library(
        _config(
            {"name": "Foo", "remote_custom_function": _SOURCE},
            {"name": "foo", "remote_custom_function": _SOURCE},
        )
    )

    assert not library.remote
    assert sorted(name for name, _why in library.failures) == ["Foo", "foo"]


def test_an_unreadable_answer_is_one_error_not_two() -> None:
    """Counts a malformed remote metric verdict as a single error."""
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(error_message="the sandbox died")}),
        _sent(_metric()),
        tally,
    )

    assert tally.by_metric == {"m": {PASSED: 0, FAILED: 0, ERRORED: 1}}
    assert tally.cases == 1


def test_a_case_the_service_ignored_charges_every_metric_an_error() -> None:
    """Records an error for each remote metric when a case is omitted."""
    page = make_page([make_agent_case("s1"), make_agent_case("s2")])
    tally = runner.Tally()

    remote.score_page_results(
        page,
        _result({"m": EvalCaseMetricResult(score=1.0)}, {}),
        _sent(_metric()),
        tally,
    )

    assert tally.by_metric == {"m": {PASSED: 1, FAILED: 0, ERRORED: 1}}
    assert tally.cases == 2
