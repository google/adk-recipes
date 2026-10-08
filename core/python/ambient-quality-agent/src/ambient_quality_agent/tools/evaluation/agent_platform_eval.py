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

"""Evaluation runner in Gemini platform for scoring agent datasets."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import agentplatform
from agentplatform._genai.types import EvaluationResult, Metric
from ambient_quality_agent.tools.evaluation.models import (
    MULTI_TURN_METRICS,
    SINGLE_TURN_METRICS,
    MetricType,
)

if TYPE_CHECKING:
    from agentplatform._genai.types import EvaluationDataset

logger = logging.getLogger(__name__)


_METRIC_REGISTRY_BY_TYPE: dict[MetricType, dict[str, Metric]] = {
    MetricType.MULTI_TURN: MULTI_TURN_METRICS,
    MetricType.SINGLE_TURN: SINGLE_TURN_METRICS,
}


def resolve_metrics(
    metrics_list: list[str], metric_type: MetricType
) -> list[Metric]:
    """Resolves metric names to eval Metric objects for an evaluation scope.

    Resolution proceeds as follows:
    1. Looks up each metric name in the registry for the specified ``metric_type``.
    2. Logs a warning and skips any metric name not present in the registry.
    3. Returns the resolved Metric instances in input order.

    Args:
        metrics_list: Metric names to evaluate (e.g. ``"task_success"``).
        metric_type: Scope of the evaluation metric (multi-turn or single-turn).

    Returns:
        The resolved `Metric` objects, in input order.
    """
    registry = _METRIC_REGISTRY_BY_TYPE[metric_type]
    resolved: list[Metric] = []
    for name in metrics_list:
        metric = registry.get(name)
        if metric is None:
            logger.warning(
                "Metric %r is not valid for %s; skipping. Known %s metrics: %s.",
                name,
                metric_type.value,
                metric_type.value,
                sorted(registry),
            )
            continue
        resolved.append(metric)
    return resolved


class AgentPlatformEvalService:
    """Evaluates agent datasets using the evaluation service in Gemini platform."""

    def __init__(self, project_id: str, location: str) -> None:
        """Initializes the eval client.

        Args:
            project_id: GCP project running the evaluation.
            location: Google Cloud region (e.g. ``"us-central1"``).
        """
        self._project_id = project_id
        self._location = location
        self._client = agentplatform.Client(
            project=self._project_id, location=self._location
        )

    def evaluate(
        self, dataset: EvaluationDataset, metrics: list[Metric]
    ) -> EvaluationResult:
        """Runs a synchronous evaluation over ``dataset``.

        Returns an empty result when ``metrics`` is empty to avoid unnecessary
        remote evaluation calls.

        Args:
            dataset: The eval cases to score.
            metrics: Resolved prebuilt metrics to evaluate.

        Returns:
            The `EvaluationResult`.
        """
        if not metrics:
            logger.info("No metrics to evaluate; skipping evaluation.")
            return EvaluationResult()

        return self._client.evals.evaluate(
            dataset=dataset,
            metrics=[self._resolve_judge_model(m) for m in metrics],
        )

    def _resolve_judge_model(self, metric: Metric) -> Metric:
        """Qualify an LLM metric's `judge_model` with this evaluator's project.

        agents-cli configs name the judge by a bare model id, but the eval
        service accepts only a full resource name, so a bare id or a
        `publishers/...` path is placed in the project and location this
        evaluator runs in.

        Args:
            metric: Metric to send.

        Returns:
            The metric, or a copy with a fully qualified `judge_model`.
        """
        model = getattr(metric, "judge_model", None)
        if not model or model.startswith("projects/"):
            return metric
        path = (
            model
            if model.startswith("publishers/")
            else f"publishers/google/models/{model}"
        )
        return metric.model_copy(
            update={
                "judge_model": (
                    f"projects/{self._project_id}/locations/{self._location}/{path}"
                )
            }
        )
