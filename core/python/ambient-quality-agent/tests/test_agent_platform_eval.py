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

"""Tests for the eval metric resolver and AgentPlatformEvalService."""

from __future__ import annotations

from collections.abc import Iterator
from unittest import mock

import pytest
from agentplatform._genai.types import EvaluationResult, PrebuiltMetric
from ambient_quality_agent.tools.evaluation.agent_platform_eval import (
    AgentPlatformEvalService,
    resolve_metrics,
)
from ambient_quality_agent.tools.evaluation.models import MetricType

_PROJECT = "test-project"
_LOCATION = "us-central1"

_CLIENT_PATH = "ambient_quality_agent.tools.evaluation.agent_platform_eval.agentplatform.Client"


@pytest.fixture
def mock_client() -> Iterator[mock.MagicMock]:
    """Patch agentplatform.Client so the evaluator uses a mock."""
    with mock.patch(_CLIENT_PATH) as client_cls:
        yield client_cls.return_value


def _make_evaluator() -> AgentPlatformEvalService:
    return AgentPlatformEvalService(project_id=_PROJECT, location=_LOCATION)


def test_resolve_metrics_multi_turn() -> None:
    resolved = resolve_metrics(
        ["task_success", "tool_use_quality", "trajectory_quality"],
        MetricType.MULTI_TURN,
    )

    assert [metric.name for metric in resolved] == [
        "MULTI_TURN_TASK_SUCCESS",
        "MULTI_TURN_TOOL_USE_QUALITY",
        "MULTI_TURN_TRAJECTORY_QUALITY",
    ]


def test_resolve_metrics_single_turn() -> None:
    resolved = resolve_metrics(
        [
            "final_response_quality",
            "hallucination",
            "tool_use_quality",
            "safety",
        ],
        MetricType.SINGLE_TURN,
    )

    assert [metric.name for metric in resolved] == [
        "FINAL_RESPONSE_QUALITY",
        "HALLUCINATION",
        "TOOL_USE_QUALITY",
        "SAFETY",
    ]


def test_resolve_metrics_tool_use_quality_is_metric_type_dependent() -> None:
    """The same name maps to different prebuilt metrics per metric type."""
    multi = resolve_metrics(["tool_use_quality"], MetricType.MULTI_TURN)
    single = resolve_metrics(["tool_use_quality"], MetricType.SINGLE_TURN)

    assert multi[0].name == "MULTI_TURN_TOOL_USE_QUALITY"
    assert single[0].name == "TOOL_USE_QUALITY"


def test_resolve_metrics_skips_wrong_metric_type_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        resolved = resolve_metrics(
            ["task_success", "safety"], MetricType.MULTI_TURN
        )

    assert [metric.name for metric in resolved] == ["MULTI_TURN_TASK_SUCCESS"]
    assert "safety" in caplog.text


def test_resolve_metrics_skips_unknown_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        resolved = resolve_metrics(
            ["task_success", "bogus_metric"], MetricType.MULTI_TURN
        )

    assert [metric.name for metric in resolved] == ["MULTI_TURN_TASK_SUCCESS"]
    assert "bogus_metric" in caplog.text


def test_resolve_metrics_empty_input() -> None:
    assert resolve_metrics([], MetricType.MULTI_TURN) == []


def test_evaluate_calls_sdk_with_resolved_metrics(
    mock_client: mock.MagicMock,
) -> None:
    evaluator = _make_evaluator()
    dataset = mock.MagicMock()
    metrics = [
        PrebuiltMetric.MULTI_TURN_TASK_SUCCESS,
        PrebuiltMetric.MULTI_TURN_TRAJECTORY_QUALITY,
    ]

    result = evaluator.evaluate(dataset, metrics)

    mock_client.evals.evaluate.assert_called_once()
    call = mock_client.evals.evaluate.call_args
    assert call.kwargs["dataset"] is dataset
    assert call.kwargs["metrics"] == metrics
    assert result is mock_client.evals.evaluate.return_value


def test_evaluate_skips_sdk_when_no_metrics(
    mock_client: mock.MagicMock,
) -> None:
    """With no metrics, the SDK is not called and an empty result is
    returned."""
    evaluator = _make_evaluator()

    result = evaluator.evaluate(mock.MagicMock(), [])

    mock_client.evals.evaluate.assert_not_called()
    assert isinstance(result, EvaluationResult)
    assert result.eval_case_results is None
    assert result.summary_metrics is None


_FULL = (
    f"projects/{_PROJECT}/locations/{_LOCATION}/publishers/google/models/m-1"
)


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("m-1", _FULL),
        ("publishers/google/models/m-1", _FULL),
        (_FULL, _FULL),
        (
            "projects/other/locations/eu/publishers/google/models/m-1",
            "projects/other/locations/eu/publishers/google/models/m-1",
        ),
        (None, None),
    ],
)
def test_a_judge_model_is_resolved_in_the_evaluator_s_project_and_location(
    mock_client: mock.MagicMock, given: str | None, sent: str | None
) -> None:
    """agents-cli configs name the judge by a bare id (`gemini-3.8-flash`); the
    eval service takes only a full resource name, so the evaluator qualifies it
    with the project and location it evaluates in."""
    from agentplatform._genai.types import LLMMetric

    metric = LLMMetric(
        name="j", prompt_template="{response}", judge_model=given
    )

    _make_evaluator().evaluate(mock.sentinel.dataset, [metric])

    (sent_metric,) = mock_client.evals.evaluate.call_args.kwargs["metrics"]
    assert sent_metric.judge_model == sent
    assert metric.judge_model == given, "the caller's metric is not mutated"
