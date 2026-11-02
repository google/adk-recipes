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

"""The per-metric-type ``eval`` workflow nodes.

A single factory ``_build_eval_node`` builds a node bound to
a specific `MetricType`; two instances — ``eval_multi_turn``
and ``eval_single_turn`` — are created at module import and
wired into the graph in series.

Each node runs the shared sweep for its own scope, so the paging lives in
`_sweep` and only the scoring is here, behind `VertexEvaluation`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core._logging import log_node_run
from ambient_quality_agent.core.nodes import _common, _sweep
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.evaluation import findings_adapter
from ambient_quality_agent.tools.evaluation.agent_platform_eval import (
    resolve_metrics,
)
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.evaluation.results import (
    build_outcome_keys,
    compute_case_outcomes,
    count_outcomes,
    count_rubrics,
)
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

if TYPE_CHECKING:
    from ambient_quality_agent.core.state import ADKStateLike
    from ambient_quality_agent.tools.ingestion.models import Page

logger = logging.getLogger(__name__)


class VertexEvaluation:
    """Scores each page with the eval service, for one metric scope."""

    def __init__(
        self, evaluator: Any, eval_metrics: Any, metric_type: MetricType
    ) -> None:
        self.name = f"{metric_type.value.lower()}_eval"
        self._evaluator = evaluator
        self._eval_metrics = eval_metrics
        self._metric_type = metric_type
        self._scope_counts = dict.fromkeys(count_outcomes([]), 0)
        self._rubrics = 0
        self._findings = 0
        self._cases = 0
        self._pages = 0
        self.outcomes: dict[str, str] = {}

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        page_index = self._pages
        self._pages += 1
        result = self._evaluator.evaluate(page.dataset, self._eval_metrics)
        logger.info("%s page %d evaluated", self.name, page_index)
        # Retain only failure findings to allow early garbage collection of the raw dataset.
        finding_set = findings_adapter.eval_results_to_finding_set(
            [result], page.revisions
        )
        # Reassign finding_sets to trigger ADK state delta change tracking.
        state["finding_sets"] = [
            *(state.get("finding_sets") or []),
            finding_set.model_dump(),
        ]
        cases = page.dataset.eval_cases or []
        # Track previously evaluated cases so missing IDs receive consistent running numbers.
        seen_before = self._cases
        self._cases += len(cases)
        self._findings += len(finding_set.findings)
        for outcome, n in count_outcomes([result]).items():
            self._scope_counts[outcome] += n
        # Map positional case indices reported by eval service back to unique trajectory keys.
        keys = build_outcome_keys(cases, seen_before)
        for case_idx, status in compute_case_outcomes([result]).items():
            position = int(case_idx)
            if 0 <= position < len(keys):
                self.outcomes[keys[position]] = status
        self._rubrics += count_rubrics([result])
        logger.info(
            "%s page %d stored: %d finding(s)",
            self.name,
            page_index,
            len(finding_set.findings),
        )

    def render_summary(self) -> str:
        return f"**{self._metric_type.value} evaluation:** {self._cases} case(s) evaluated."

    @property
    def findings_count(self) -> int:
        return self._findings

    @property
    def rubrics_count(self) -> int:
        return self._rubrics

    def finish(self, state: ADKStateLike) -> None:
        running = state.get("eval_outcome_counts") or {}
        state["eval_outcome_counts"] = {
            outcome: running.get(outcome, 0) + self._scope_counts[outcome]
            for outcome in self._scope_counts
        }


def _build_eval_node(metric_type: MetricType):
    """Build the ``eval_<scope>`` workflow node bound to ``metric_type``.

    The node skips when no metrics are selected for this scope or
    ``budget_per_metric`` is zero. Otherwise the shared sweep pages the window
    for this scope and hands every page to its `VertexEvaluation`.

    Args:
        metric_type: Metric scope type (MULTI_TURN or SINGLE_TURN).

    Returns:
        Configured ADK workflow node.
    """
    node_name = f"eval_{metric_type.value.lower()}"
    metrics_key = f"{metric_type.value.lower()}_metrics"

    def run_eval(ctx: Context) -> Event:
        state = ctx.state
        metrics: list[str] = list(state.get(metrics_key) or [])
        budget = int(state.get("budget_per_metric", 0) or 0)

        if not metrics or budget <= 0:
            logger.warning(
                "%s: no metrics or zero budget; skipping.", node_name
            )
            return WorkflowState.record_progress_event(
                state,
                f"**{metric_type.value} evaluation:** "
                f"_Skipped (no metrics configured or zero budget)._\n",
                node_name,
            )

        evaluation = VertexEvaluation(
            _common.evaluator_factory(ctx),
            resolve_metrics(metrics, metric_type),
            metric_type,
        )
        lines = _sweep.run_sweep(
            ctx,
            [evaluation],
            node_name=node_name,
            budget=budget,
            metric_type=metric_type,
        )
        return WorkflowState.record_progress_event(
            state, lines[0] + "\n", node_name
        )

    return node(log_node_run(node_name)(run_eval), name=node_name)


eval_multi_turn_node = _build_eval_node(MetricType.MULTI_TURN)
eval_single_turn_node = _build_eval_node(MetricType.SINGLE_TURN)
