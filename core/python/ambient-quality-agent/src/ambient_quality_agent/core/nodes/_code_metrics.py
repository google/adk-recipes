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

"""Score multi-turn telemetry sessions against local Python metrics for insight
correlation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.metrics import runner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ambient_quality_agent.tools.ingestion.models import Page
    from ambient_quality_agent.tools.metrics.library import (
        CodeMetric,
        MetricLibrary,
    )

logger = logging.getLogger(__name__)

MODULE_NAME = "custom_metrics"


class CodeMetricEvaluation:
    """Scores each page against the published code-metric library."""

    name = "custom_metrics"

    @property
    def outcomes(self) -> dict[str, str]:
        return self._tally.outcomes

    @property
    def findings_count(self) -> int:
        return self._tally.findings

    @property
    def rubrics_count(self) -> int:
        return 0

    def __init__(self, library: MetricLibrary) -> None:
        self._library = library
        self._tally = runner.Tally()

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        # Skip evaluation if the library has no local metrics to avoid failing finish()
        # on empty scored metrics when individual sessions fail build_instance.
        if not self._library:
            return
        finding_set = runner.score_page(page, self._library, self._tally)
        # Reassign finding_sets to trigger ADK state change detection.
        state["finding_sets"] = [
            *(state.get("finding_sets") or []),
            finding_set.model_dump(),
        ]
        logger.info(
            "%s stored %d finding(s)", MODULE_NAME, len(finding_set.findings)
        )

    def finish(self, state: ADKStateLike) -> None:
        tally = self._tally
        runner.merge_breakdown(state, tally.by_metric, kind="local")
        runner.merge_metrics_detail(
            state,
            {
                **{
                    name: {"kind": kind, "not_run": why}
                    for name, why, kind in self._library.declined
                },
                **{
                    name: {"kind": "refused", "not_run": why}
                    for name, why in self._library.failures
                },
                **{
                    metric.name: {
                        "kind": "local",
                        "expected": metric.expected,
                        "threshold": metric.threshold,
                        "model_modules": list(metric.model_modules),
                    }
                    for metric in self._library.values()
                },
            },
        )
        # Fail the sweep if no metrics returned a verdict, indicating zero measurements occurred.
        if tally.first_error is not None and not tally.scored_metrics:
            raise RuntimeError(
                "No code metric returned a verdict on any session."
            ) from tally.first_error

    def render_summary(self) -> str:
        return _render_code_metrics_summary(
            self._library, self._tally, truncated=False
        )


def _render_code_metrics_summary(
    library: Mapping[str, CodeMetric], tally: runner.Tally, truncated: bool
) -> str:
    """Format a markdown summary of scored sessions, findings, and dead metrics.

    Args:
        library: Mapping of metric names to `CodeMetric` definitions.
        tally: Tally accumulator tracking evaluation outcomes.
        truncated: Whether execution stopped early near memory limits.

    Returns:
        Formatted markdown summary line.
    """
    text = (
        f"{tally.cases} session(s) scored against {len(library)} metric(s), "
        f"{tally.findings} finding(s)"
    )
    if truncated:
        text += " (stopped early near the memory limit)"
    dead = sorted(set(library) - tally.scored_metrics)
    if dead:
        text += (
            f". **{len(dead)} metric(s) never returned a verdict** and found "
            f"nothing: {', '.join(f'`{name}`' for name in dead)}"
        )
    return text + "." + _render_model_cost_note(library, tally.cases)


def _render_model_cost_note(
    library: Mapping[str, CodeMetric], cases: int
) -> str:
    """Note metrics that import model clients and report their incurred cost.

    Model calls live inside metric code and cannot be seen in config. We report
    actual sessions evaluated so operators see a realized bill, not a projection.

    Args:
        library: Mapping of metric names to `CodeMetric` definitions.
        cases: Count of evaluated cases.

    Returns:
        Formatted note string, or empty string if no metrics call models.
    """
    costly = {
        name: metric.model_modules
        for name, metric in library.items()
        if metric.model_modules
    }
    if not costly:
        return ""
    named = ", ".join(
        f"`{name}` ({', '.join(mods)})" for name, mods in sorted(costly.items())
    )
    return (
        f" **{len(costly)} metric(s) import a model client** and cost a model "
        f"call per session, on this engine's credentials, over and above the "
        f"session reviewer: {named}. {cases} session(s) went through them this "
        f"sweep."
    )
