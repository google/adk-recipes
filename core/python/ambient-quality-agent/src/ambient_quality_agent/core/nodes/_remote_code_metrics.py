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

"""Score multi-turn telemetry sessions with metrics the eval service runs or judges.

The sibling of `_code_metrics`, which runs the library's local metrics in this
process. Both produce the same findings from the same pages; this one hands the
eval service the author's source to execute, or prompt template to judge,
instead of importing anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.metrics import remote, runner
from ambient_quality_agent.tools.metrics.library import (
    JUDGED_KIND,
    REMOTE_KIND,
    RemoteJudgeMetric,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ambient_quality_agent.tools.ingestion.models import Page
    from ambient_quality_agent.tools.metrics.library import RemoteMetric

logger = logging.getLogger(__name__)

MODULE_NAME = "remote_custom_metrics"


class RemoteCodeMetricEvaluation:
    """Scores each page with the metrics the eval service runs or judges.

    Named for the code metrics it first carried. Judged metrics share it rather
    than getting a twin because both kinds travel in the one `evaluate` call
    and come back in the one result set; splitting them would page the window
    twice for nothing.
    """

    name = "remote_custom_metrics"

    @property
    def outcomes(self) -> dict[str, str]:
        return self._tally.outcomes

    @property
    def findings_count(self) -> int:
        return self._tally.findings

    @property
    def rubrics_count(self) -> int:
        # Service functions and judge prompts return numerical scores rather
        # than grading individual rubrics.
        return 0

    def __init__(
        self,
        evaluator: Any | Callable[[], Any],
        metrics: Sequence[RemoteMetric],
    ) -> None:
        # Accept a factory to defer SDK credential resolution to first page execution,
        # ensuring early credential errors are captured in page outcomes rather than crashing initialization.
        self._evaluator = evaluator
        # Key metrics by service-returned lowercase names to match SDK response payloads.
        self._metrics = {metric.service_name: metric for metric in metrics}
        # The tally is keyed by config spelling, so the kind is looked up by it.
        self._kinds = {metric.name: metric.kind for metric in metrics}
        self._eval_metrics = remote.build_eval_metrics(metrics)
        self._tally = runner.Tally()

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        # Dispatch one API request per page to reflect true metric cost;
        # datasets are reconstructed so the service evaluates local runner instances.
        try:
            result = self._resolve_evaluator().evaluate(
                remote.build_request_dataset(page.dataset), self._eval_metrics
            )
        # Capture evaluation exceptions so peer analyzers can continue and run counters are written;
        # marks page errored and finish() will raise if zero metrics score across the sweep.
        except Exception as exc:
            logger.exception("%s: the eval service call failed.", MODULE_NAME)
            remote.mark_page_errored(
                page, self._tally, exc, self._metrics.values()
            )
            return
        finding_set = remote.score_page_results(
            page, result, self._metrics, self._tally
        )
        # Reassign finding_sets list to trigger ADK state change detection.
        state["finding_sets"] = [
            *(state.get("finding_sets") or []),
            finding_set.model_dump(),
        ]
        logger.info(
            "%s stored %d finding(s)", MODULE_NAME, len(finding_set.findings)
        )

    def _resolve_evaluator(self) -> Any:
        """Resolve and instantiate evaluator on first use if a factory was provided.

        Returns:
            Instantiated evaluator object.
        """
        if callable(self._evaluator) and not hasattr(
            self._evaluator, "evaluate"
        ):
            self._evaluator = self._evaluator()
        return self._evaluator

    def finish(self, state: ADKStateLike) -> None:
        tally = self._tally
        for kind in (REMOTE_KIND, JUDGED_KIND):
            by_metric = {
                name: counts
                for name, counts in tally.by_metric.items()
                if self._kinds.get(name) == kind
            }
            if by_metric:
                runner.merge_breakdown(state, by_metric, kind=kind)
        runner.merge_metrics_detail(
            state,
            {
                metric.name: {
                    "kind": metric.kind,
                    "expected": metric.expected,
                    "threshold": metric.threshold,
                    "model_modules": [],
                    **(
                        {"scores": tally.scores[metric.name]}
                        if metric.name in tally.scores
                        else {}
                    ),
                }
                for metric in self._metrics.values()
            },
        )
        # Raise when sessions were processed but zero verdicts reached, preventing silent failures.
        if tally.cases and not tally.scored_metrics:
            raise RuntimeError(
                "No remote metric returned a verdict on any session."
            ) from tally.first_error

    def render_summary(self) -> str:
        tally = self._tally
        text = (
            f"**Remote custom metrics:** {tally.cases} session(s) scored against "
            f"{len(self._metrics)} metric(s) on the eval service, "
            f"{tally.findings} finding(s)"
        )
        # A judge costs one autorater call per session per sample, per sweep.
        judges = [
            m
            for m in self._metrics.values()
            if isinstance(m, RemoteJudgeMetric)
        ]
        if judges:
            calls = sum(
                m.metric.judge_model_sampling_count or 1 for m in judges
            )
            text += (
                f" ({len(judges)} judged by the eval service's model, "
                f"{calls} model call(s) per session)"
            )
        # Display dead metrics using configuration names as recognized by operators.
        dead = sorted(self._kinds.keys() - tally.scored_metrics)
        if dead:
            text += (
                f". **{len(dead)} metric(s) never returned a verdict** and found "
                f"nothing: {', '.join(f'`{name}`' for name in dead)}"
            )
        return text + "."
