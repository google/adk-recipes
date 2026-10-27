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

"""Tests for metrics the eval service judges from a prompt template.

The judged path shares its transport with the remote code path: one
`evaluate` call per page, the dataset `remote.build_request_dataset` builds (its
response derivation, silent-session and copy-on-write behaviour are pinned in
`test_remote_code_metrics`), and the same positional join and tally. What is
tested here is what differs: how a `prompt_template` entry loads, what is sent
for it, and how its verdict reads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentplatform._genai.types import (
    CodeExecutionMetric,
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationResult,
    LLMMetric,
    ResponseCandidateResult,
)
from ambient_quality_agent.core.nodes._remote_code_metrics import (
    RemoteCodeMetricEvaluation,
)
from ambient_quality_agent.tools.metrics import remote, runner
from ambient_quality_agent.tools.metrics.library import (
    RemoteCodeMetric,
    RemoteJudgeMetric,
    load_library,
)

from .conftest import make_agent_case, make_page

_TEMPLATE = "Is the reply polite? {response}"
_SOURCE = "def evaluate(instance):\n    return 1.0\n"


def _config(
    *entries: dict[str, Any], run: list[str] | None = None
) -> dict[str, bytes]:
    """An eval config publishing `entries`, as a GCS object mapping."""
    names = run if run is not None else [e["name"] for e in entries]
    body = {"metrics_to_run": names, "custom_metrics": list(entries)}
    return {"eval_config.json": json.dumps(body).encode()}


def _judge(
    name: str = "j", threshold: float | None = 1.0, samples: int | None = None
) -> RemoteJudgeMetric:
    """A judged metric whose `service_name` the SDK resolved, as the loader does."""
    metric = LLMMetric(
        name=name, prompt_template=_TEMPLATE, judge_model_sampling_count=samples
    )
    return RemoteJudgeMetric(
        name=name,
        service_name=metric.name or name,
        expected="The agent is polite.",
        metric=metric,
        threshold=threshold,
    )


def _code(name: str = "m") -> RemoteCodeMetric:
    """A remote code metric, as `test_remote_code_metrics._metric` builds one."""
    return RemoteCodeMetric(
        name=name,
        service_name=CodeExecutionMetric(
            name=name, custom_function=_SOURCE
        ).name
        or name,
        expected="The agent answers.",
        source=_SOURCE,
    )


def _result(*metrics: dict[str, EvalCaseMetricResult]) -> EvaluationResult:
    """One page of results, one case per mapping given, keyed as the service
    keys them -- lowercased."""
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


class _Evaluator:
    """Stands in for the eval service, recording what it was asked to run."""

    def __init__(self, result: EvaluationResult) -> None:
        self._result = result
        self.calls: list[Any] = []

    def evaluate(self, dataset: Any, metrics: Any) -> EvaluationResult:
        self.calls.append((dataset, metrics))
        return self._result


# --- loading ----------------------------------------------------------------- #


def test_a_prompt_template_alone_is_loaded_as_a_judged_metric() -> None:
    """The entry that was declined as "handled by session review" -- which
    never read it -- now names a metric the eval service judges."""
    library = load_library(
        _config({"name": "polite", "prompt_template": _TEMPLATE})
    )

    assert not library and not library.remote
    assert [m.name for m in library.judged] == ["polite"]
    assert library.judged[0].metric.prompt_template == _TEMPLATE
    assert not library.declined and not library.failures


def test_the_loader_resolves_the_name_the_service_answers_under_for_a_judge() -> (
    None
):
    """The same join hazard the code path solved: the SDK lowercases the name
    and keys every result by the lowered form."""
    library = load_library(
        _config({"name": "AnswerIsPolite", "prompt_template": _TEMPLATE})
    )

    (judge,) = library.judged
    assert judge.name == "AnswerIsPolite"
    assert judge.service_name == "answerispolite"
    assert judge.metric.name == "answerispolite"


@pytest.mark.parametrize("template", ["", "   ", None])
def test_a_blank_prompt_template_is_a_named_failure_not_a_dead_metric(
    template: str | None,
) -> None:
    """`agents-cli` refuses these through the same SDK validator."""
    library = load_library(
        _config({"name": "blank", "prompt_template": template})
    )

    assert not library.judged
    assert [name for name, _why in library.failures] == ["blank"]
    assert "Prompt template" in library.failures[0][1]


def test_a_judged_metric_carries_its_authors_threshold() -> None:
    library = load_library(
        _config({"name": "m", "prompt_template": _TEMPLATE, "threshold": 3})
    )

    assert library.judged[0].threshold == pytest.approx(3.0)


def test_a_non_finite_threshold_on_a_judge_is_refused() -> None:
    """`score >= nan` is false for every score: a defect storm, not a typo."""
    library = load_library(
        _config(
            {
                "name": "m",
                "prompt_template": _TEMPLATE,
                "threshold": float("nan"),
            }
        )
    )

    assert not library.judged
    (name, why) = library.failures[0]
    assert name == "m"
    assert "non-finite" in why


def test_a_judged_metric_reads_its_contract_from_the_config() -> None:
    """Findings cluster on `expected`, and a prompt has no `EXPECTED` literal
    to read it off -- so the config carries it, as it carries `threshold`."""
    library = load_library(
        _config(
            {
                "name": "polite",
                "prompt_template": _TEMPLATE,
                "expected": "  The agent stays courteous.  ",
            }
        )
    )

    assert library.judged[0].expected == "The agent stays courteous."


@pytest.mark.parametrize("extra", [{}, {"expected": 12}, {"expected": "  "}])
def test_a_judged_metric_without_a_usable_expected_falls_back_to_its_name(
    extra: dict[str, Any],
) -> None:
    library = load_library(
        _config({"name": "polite", "prompt_template": _TEMPLATE, **extra})
    )

    assert not library.failures
    assert (
        library.judged[0].expected == "The agent satisfies the 'polite' metric."
    )


def test_a_judge_and_a_function_that_the_service_would_name_alike_are_both_refused() -> (
    None
):
    """Both buckets travel in one `evaluate` call, so the service's one result
    for `foo` is as ambiguous across kinds as it is within one."""
    library = load_library(
        _config(
            {"name": "Foo", "prompt_template": _TEMPLATE},
            {"name": "foo", "remote_custom_function": _SOURCE},
        )
    )

    assert not library.judged and not library.remote
    assert sorted(name for name, _why in library.failures) == ["Foo", "foo"]


_JUDGE_MODEL = (
    "projects/p/locations/l/publishers/google/models/gemini-2.5-flash"
)


def test_judge_settings_pass_through_to_the_sdk_metric() -> None:
    """Whatever `agents-cli` passes to `LLMMetric.model_validate`, so does this."""
    library = load_library(
        _config(
            {
                "name": "m",
                "prompt_template": _TEMPLATE,
                "judge_model": _JUDGE_MODEL,
                "judge_model_sampling_count": 3,
                "judge_model_system_instruction": "Be strict.",
                "return_raw_output": False,
            }
        )
    )

    metric = library.judged[0].metric
    assert metric.judge_model == _JUDGE_MODEL
    assert metric.judge_model_sampling_count == 3
    assert metric.judge_model_system_instruction == "Be strict."
    assert metric.return_raw_output is False


def test_an_out_of_range_sampling_count_is_refused_as_agents_cli_refuses_it() -> (
    None
):
    library = load_library(
        _config(
            {
                "name": "m",
                "prompt_template": _TEMPLATE,
                "judge_model_sampling_count": 99,
            }
        )
    )

    assert not library.judged
    (name, why) = library.failures[0]
    assert name == "m"
    assert "judge_model_sampling_count" in why


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        (
            {"prompt_template": f"{_TEMPLATE} Turns: {{agent_eval_data}}"},
            "{agent_eval_data}",
        ),
        ({"return_raw_output": True}, "return_raw_output"),
    ],
)
def test_a_judge_shape_the_service_errors_on_every_session_is_refused_at_load(
    entry: dict[str, Any], said: str
) -> None:
    """Measured live: each of these 400s on every case, so the metric never
    reaches a verdict and the run fails. Refused here by name instead, as a
    remote function that visibly returns a mapping is."""
    library = load_library(
        _config({"name": "m", "prompt_template": _TEMPLATE, **entry})
    )

    assert not library.judged
    (name, why) = library.failures[0]
    assert name == "m"
    assert said in why


def test_a_template_asking_for_the_prompt_loads() -> None:
    """The SDK's own `MetricPromptBuilder` writes `{prompt}` into every
    template, and the request carries a prompt to render it with."""
    template = f"User: {{prompt}}\n{_TEMPLATE}"

    library = load_library(_config({"name": "m", "prompt_template": template}))

    assert not library.failures
    assert library.judged[0].metric.prompt_template == template


def test_aqua_only_keys_do_not_reach_the_request() -> None:
    """`threshold` and `expected` ride on the `LLMMetric` as pydantic extras.
    Only the SDK's transformer keeps them out of the payload; e2e proved it
    once, this pins it against an SDK that starts serialising extras."""
    from agentplatform._genai import _transformers

    library = load_library(
        _config(
            {
                "name": "m",
                "prompt_template": _TEMPLATE,
                "threshold": 0.5,
                "expected": "Polite.",
            }
        )
    )

    (payload,) = _transformers.t_metrics(
        remote.build_eval_metrics(library.judged)
    )

    assert (
        payload["llm_based_metric_spec"]["metric_prompt_template"] == _TEMPLATE
    )
    assert not {"threshold", "expected"} & payload.keys()
    assert (
        not {"threshold", "expected"} & payload["llm_based_metric_spec"].keys()
    )


# --- the metrics sent to the service ------------------------------------------ #


def test_each_metric_is_sent_as_the_sdk_type_the_service_runs_it_with() -> None:
    """A judge travels as the very `LLMMetric` the loader validated; a function
    is wrapped as before. Both in one list, for one call."""
    j, m = _judge("j"), _code("m")

    sent = remote.build_eval_metrics([j, m])

    assert [type(x) for x in sent] == [LLMMetric, CodeExecutionMetric]
    assert sent[0] is j.metric and sent[0].name == "j"
    assert sent[1].custom_function == _SOURCE


def test_the_sdk_routes_a_judged_metric_to_the_llm_handler() -> None:
    """The judge runs server-side, never through `CustomMetricHandler`, which
    would call a function in this process on the agent's credentials."""
    from agentplatform._genai import _evals_metric_handlers as handlers

    metric = remote.build_eval_metrics([_judge()])[0]

    matched = [
        h for cond, h in handlers._METRIC_HANDLER_MAPPING if cond(metric)
    ]

    assert matched[0] is handlers.LLMMetricHandler


# --- scoring ------------------------------------------------------------------ #


def _sent(*metrics: RemoteJudgeMetric | RemoteCodeMetric) -> dict[str, Any]:
    """The lookup `score_page_results` takes, keyed as the evaluation keys it."""
    return {m.service_name: m for m in metrics}


def test_a_failing_judge_verdict_becomes_a_finding_with_the_judges_explanation() -> (
    None
):
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result(
            {
                "j": EvalCaseMetricResult(
                    score=0.0, explanation="The reply is curt."
                )
            }
        ),
        _sent(_judge("j")),
        tally,
    )

    assert [f.actual_behavior for f in finding_set.findings] == [
        "The reply is curt."
    ]
    assert [f.expected_behavior for f in finding_set.findings] == [
        "The agent is polite."
    ]
    assert (tally.cases, tally.failed, tally.passed) == (1, 1, 0)


def test_a_judge_that_returned_no_explanation_is_reported_as_unexplained() -> (
    None
):
    """The code-metric sentence says the service never returns an explanation,
    which is false for a judge -- so a judge keeps the local wording."""
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result({"j": EvalCaseMetricResult(score=0.0)}),
        _sent(_judge("j")),
        tally,
    )

    (actual,) = [f.actual_behavior for f in finding_set.findings]
    assert "eval service does not return an explanation" not in actual
    assert actual == "The metric failed but returned no explanation."


def test_a_code_metric_beside_a_judge_keeps_the_code_metric_sentence() -> None:
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    finding_set = remote.score_page_results(
        page,
        _result(
            {
                "m": EvalCaseMetricResult(score=0.0),
                "j": EvalCaseMetricResult(score=1.0),
            }
        ),
        _sent(_code("m"), _judge("j")),
        tally,
    )

    (actual,) = [f.actual_behavior for f in finding_set.findings]
    assert "eval service does not return an explanation" in actual


# --- the evaluation in the sweep ---------------------------------------------- #


def test_a_judged_metric_is_described_as_judged_beside_a_remote_one() -> None:
    """`metrics_detail.kind` is what the dashboard labels the row by, and
    `metrics_by_name` counts both kinds."""
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(
            _result(
                {
                    "j": EvalCaseMetricResult(score=0.0, explanation="curt"),
                    "m": EvalCaseMetricResult(score=1.0),
                }
            )
        ),
        [_judge("j"), _code("m")],
    )
    state: dict[str, Any] = {}

    evaluation.evaluate(state, make_page([make_agent_case("s1")]))
    evaluation.finish(state)

    assert state["metrics_detail"]["j"] == {
        "kind": "judged",
        "expected": "The agent is polite.",
        "threshold": 1.0,
        "model_modules": [],
    }
    assert state["metrics_detail"]["m"]["kind"] == "remote"
    assert state["metrics_by_name"] == {
        "j": {"passed": 0, "failed": 1, "errored": 0},
        "m": {"passed": 1, "failed": 0, "errored": 0},
    }


def test_a_judged_only_library_that_never_scored_fails_the_run() -> None:
    """Same contract as a code metric: silence is not a clean sweep."""
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(EvaluationResult()), [_judge()]
    )
    state: dict[str, Any] = {}

    evaluation.evaluate(state, make_page([make_agent_case("s1")]))

    assert evaluation.outcomes == {"s1": "errored"}
    with pytest.raises(
        RuntimeError, match="No remote metric returned a verdict"
    ):
        evaluation.finish(state)


def test_the_summary_names_how_many_metrics_the_autorater_judged() -> None:
    """A judged metric costs a model call per session; the summary says so, as
    the local path names metrics that import a model client."""
    answer = _result(
        {
            "j": EvalCaseMetricResult(score=1.0),
            "m": EvalCaseMetricResult(score=1.0),
        }
    )
    mixed = RemoteCodeMetricEvaluation(
        _Evaluator(answer), [_judge("j"), _code("m")]
    )
    code_only = RemoteCodeMetricEvaluation(_Evaluator(answer), [_code("m")])
    page = make_page([make_agent_case("s1")])
    mixed.evaluate({}, page)
    code_only.evaluate({}, page)

    assert (
        "1 judged by the eval service's model, 1 model call(s) per session"
        in mixed.render_summary()
    )
    assert "judged by" not in code_only.render_summary()


def test_the_summary_counts_each_judge_sample_as_a_call() -> None:
    """`judge_model_sampling_count` asks the autorater that many times per
    session, so the stated cost is the sum, not one per metric."""
    answer = _result(
        {
            "a": EvalCaseMetricResult(score=1.0),
            "b": EvalCaseMetricResult(score=1.0),
        }
    )
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(answer), [_judge("a"), _judge("b", samples=4)]
    )
    evaluation.evaluate({}, make_page([make_agent_case("s1")]))

    assert (
        "2 judged by the eval service's model, 5 model call(s) per session"
        in evaluation.render_summary()
    )


# --- scores only, as agents-cli reports a judge ---------------------------- #


def test_a_judge_without_a_threshold_loads_as_scores_only() -> None:
    """agents-cli has no pass mark for a judge, so a config written for it sets
    none; AQuA then reports the scores and files no findings."""
    library = load_library(_config({"name": "m", "prompt_template": _TEMPLATE}))

    assert library.judged[0].threshold is None


def test_a_scores_only_judge_files_no_finding_and_passes_the_session() -> None:
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()
    judge = _judge("j", threshold=None)

    finding_set = remote.score_page_results(
        page,
        _result({"j": EvalCaseMetricResult(score=2.0, explanation="meh")}),
        _sent(judge),
        tally,
    )

    assert finding_set.findings == []
    assert tally.outcomes and set(tally.outcomes.values()) == {"passed"}
    assert tally.by_metric["j"] == {
        "passed": 0,
        "failed": 0,
        "errored": 0,
        "scored": 1,
    }
    assert "j" in tally.scored_metrics


def test_a_scores_only_judge_that_errored_is_still_an_error() -> None:
    page, tally = make_page([make_agent_case("s1")]), runner.Tally()

    remote.score_page_results(
        page,
        _result({"j": EvalCaseMetricResult(error_message="boom")}),
        _sent(_judge("j", threshold=None)),
        tally,
    )

    assert tally.by_metric["j"]["errored"] == 1
    assert set(tally.outcomes.values()) == {"errored"}


def test_the_score_summary_lands_in_the_metric_detail() -> None:
    answer = EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=i,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={"j": EvalCaseMetricResult(score=s)},
                    )
                ],
            )
            for i, s in enumerate([1.0, 5.0, 3.0])
        ]
    )
    evaluation = RemoteCodeMetricEvaluation(
        _Evaluator(answer), [_judge("j", threshold=None)]
    )
    state: dict[str, Any] = {}
    evaluation.evaluate(
        state, make_page([make_agent_case(f"s{i}") for i in range(3)])
    )
    evaluation.finish(state)

    detail = state["metrics_detail"]["j"]
    assert detail["threshold"] is None
    assert detail["scores"] == {"count": 3, "mean": 3.0, "min": 1.0, "max": 5.0}
    assert state["metrics_by_name"]["j"]["scored"] == 3
