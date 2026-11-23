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
from agentplatform._genai.types import (
    EvalCase,
    EvaluationDataset,
    EvaluationResult,
    PrebuiltMetric,
)
from agentplatform._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.tools.evaluation.agent_platform_eval import (
    AgentPlatformEvalService,
    resolve_metrics,
)
from ambient_quality_agent.tools.evaluation.models import MetricType
from google.genai.types import Content, FileData, Part

_PROJECT = "test-project"
_LOCATION = "us-central1"

_CLIENT_PATH = "agentplatform.Client"


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
    dataset = EvaluationDataset(eval_cases=[EvalCase(eval_case_id="c1")])
    metrics = [
        PrebuiltMetric.MULTI_TURN_TASK_SUCCESS,
        PrebuiltMetric.MULTI_TURN_TRAJECTORY_QUALITY,
    ]

    result = evaluator.evaluate(dataset, metrics)

    mock_client.evals.evaluate.assert_called_once()
    call = mock_client.evals.evaluate.call_args
    assert call.kwargs["dataset"] == dataset
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

    _make_evaluator().evaluate(EvaluationDataset(eval_cases=[]), [metric])

    (sent_metric,) = mock_client.evals.evaluate.call_args.kwargs["metrics"]
    assert sent_metric.judge_model == sent
    assert metric.judge_model == given, "the caller's metric is not mutated"


_TRAJECTORY_FILE_URI = (
    "gs://jobs/aqa-trajectory-files/agent-key/2026-11-10/" + "a" * 64
)


def _build_case_with_link(case_id: str, file_uri: str) -> EvalCase:
    """Builds a case whose one user message links to a file.

    Args:
        case_id: The case id.
        file_uri: The link.

    Returns:
        The case.
    """
    return EvalCase(
        eval_case_id=case_id,
        agent_data=AgentData(
            turns=[
                ConversationTurn(
                    turn_index=0,
                    events=[
                        AgentEvent(
                            author="user",
                            content=Content(
                                role="user",
                                parts=[
                                    Part(text="Look:"),
                                    Part(
                                        file_data=FileData(
                                            file_uri=file_uri,
                                            mime_type="application/octet-stream",
                                        )
                                    ),
                                ],
                            ),
                        )
                    ],
                )
            ]
        ),
    )


def _list_part_texts(case: EvalCase) -> list[str | None]:
    assert case.agent_data is not None
    return [
        part.text if part.text is not None else part.file_data.file_uri
        for turn in case.agent_data.turns or []
        for event in turn.events or []
        for part in event.content.parts or []
    ]


def test_evaluate_sends_trajectory_file_links_as_placeholders_in_case_order(
    mock_client: mock.MagicMock,
) -> None:
    """The judge model gets every `file_data` link, and one it cannot fetch
    or one of an unsupported type fails every metric of the case."""
    dataset = EvaluationDataset(
        eval_cases=[
            _build_case_with_link("linked", _TRAJECTORY_FILE_URI),
            _build_case_with_link("agent-sent", "gs://agent/report.pdf"),
            EvalCase(eval_case_id="empty"),
        ]
    )

    _make_evaluator().evaluate(
        dataset, [PrebuiltMetric.MULTI_TURN_TASK_SUCCESS]
    )

    sent = mock_client.evals.evaluate.call_args.kwargs["dataset"]
    assert [case.eval_case_id for case in sent.eval_cases] == [
        "linked",
        "agent-sent",
        "empty",
    ]
    assert _list_part_texts(sent.eval_cases[0]) == [
        "Look:",
        "<attachment: application/octet-stream>",
    ]
    assert _list_part_texts(sent.eval_cases[1]) == [
        "Look:",
        "gs://agent/report.pdf",
    ]
    assert dataset.eval_cases is not None
    assert _list_part_texts(dataset.eval_cases[0]) == [
        "Look:",
        _TRAJECTORY_FILE_URI,
    ], "the caller's dataset keeps its links"


def test_evaluate_passes_a_dataset_without_cases_through(
    mock_client: mock.MagicMock,
) -> None:
    """A dataset given as a GCS source holds no `eval_cases` to clean."""
    dataset = EvaluationDataset(gcs_source={"uris": ["gs://b/cases.jsonl"]})

    _make_evaluator().evaluate(
        dataset, [PrebuiltMetric.MULTI_TURN_TASK_SUCCESS]
    )

    sent = mock_client.evals.evaluate.call_args.kwargs["dataset"]
    assert sent is dataset
    assert sent.eval_cases is None
