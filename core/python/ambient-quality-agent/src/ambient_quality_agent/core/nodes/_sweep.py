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

"""Ingest a window once, and hand every page to each enabled evaluation.

Wiring::

    run_sweep()
        │ _common.fetcher_factory()
        ▼
    BaseFetcher ◄── _common.refuse_unvalidated_selector(), _common.count_scanned()
        │ submit_query(), then fetch_page() until Page.is_exhausted
        ▼
    Page ── evaluate() ──► every Evaluation (Protocol) in the list
        │
        ▼
    _common.add_ingestion_counts()
        │
        ├── no pages, deployed ──► render_nothing_evaluated_line()
        ├── no traces, standalone ──► _common.count_by_agent()
        │                             ──► raise EmptyWindowError
        ▼
    collapse_outcomes() ◄── Evaluation.outcomes
        │ build_pages_key()
        ▼
    add_counts() ──► state counters
        │
        ▼
    Evaluation.finish(), Evaluation.render_summary() ──► summary lines

Separates fetching from scoring. The producers each grew their own copy of the
same fetch loop, so enabling two of them queried and paged the same window
twice and paid the ingestion cost twice.

Fetching stays in this module and scoring moves behind `Evaluation`, so a new
way to judge a trajectory is a class here, not another node with another loop.

Pages are handed to the evaluations as they arrive and are not accumulated.
Paging exists because a window does not fit in memory, so anything that held
the pages to score them afterwards would give that up.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from typing import TYPE_CHECKING, Protocol

from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import add_counts
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    OUTCOME_RANK,
    PASSED,
)
from ambient_quality_agent.tools.ingestion.base import AGENT_COUNT_LIMIT

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ambient_quality_agent.core.state import ADKStateLike
    from ambient_quality_agent.tools.ingestion.models import Page
    from google.adk.agents.context import Context

logger = logging.getLogger(__name__)


class EmptyWindowError(RuntimeError):
    """A standalone sweep found no traces at all for the observed agent.

    Deployed, an empty window is a warning: AQuA cannot know the agent's traffic
    pattern, and an idle hour must not fail a scheduled run. A standalone run is
    started by hand to see what AQuA finds, so a window with nothing in it is
    almost always a setting to fix, and failing names the setting.
    """


class Evaluation(Protocol):
    """One way of judging a page of trajectories.

    Implementations append their findings to `state["finding_sets"]`, which is
    the single shape `insight_correlation` consumes, so adding an evaluation
    needs no change downstream.
    """

    name: str

    @property
    def outcomes(self) -> dict[str, str]:
        """The verdict this evaluation reached on each trajectory it judged.

        Keyed by `eval_case_id`, falling back to ``#`` and the trajectory's
        running number in `page.dataset.eval_cases` order when one carries no
        id. The sweep merges these keys across evaluations to count a trajectory
        once, so an evaluation naming one differently from its peers turns a
        single trajectory into several and restores the double-count this
        collapse exists to prevent. Key on the trajectory, never on a position
        alone.
        """
        ...

    @property
    def findings_count(self) -> int: ...

    @property
    def rubrics_count(self) -> int: ...

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        """Judge one page and record what it found."""

    def render_summary(self) -> str:
        """One markdown line for the run's progress event."""

    def finish(self, state: ADKStateLike) -> None:
        """Finalize evaluation state and raise if it failed outright."""


def run_sweep(
    ctx: Context,
    evaluations: list[Evaluation],
    *,
    node_name: str,
    budget: int,
    metric_type: MetricType = MetricType.MULTI_TURN,
) -> list[str]:
    """Page the telemetry window once and pass each page to every active evaluation.

    Args:
        ctx: Workflow context supplying state and configuration.
        evaluations: Evaluation passes to run over each ingested page.
        node_name: Node name for logging and error reporting.
        budget: Maximum number of sessions or traces to ingest.
        metric_type: Ingestion scope (multi-turn sessions or single-turn traces).

    Returns:
        Summary lines from each evaluation for progress reporting.

    Raises:
        selector.SelectorRefused: If the run carries an invalid or unauthorized
            SQL selector before any query executes.
        EmptyWindowError: If a standalone run's window holds no traces for the
            observed agent, and no selector narrowed it.
        Exception: Propagates any terminal evaluation failure from `finish`.
    """
    state = ctx.state
    fetcher = _common.fetcher_factory(ctx)
    start = dt.datetime.fromisoformat(state["window_start"])
    end = dt.datetime.fromisoformat(state["window_end"])
    agent_name = state["observed_agent_name"]

    _common.refuse_unvalidated_selector(
        fetcher,
        node_name=node_name,
        agent_name=agent_name,
        start=start,
        end=end,
        limit=budget,
    )
    scanned = _common.count_scanned(
        fetcher,
        node_name=node_name,
        agent_name=agent_name,
        metric_type=metric_type,
        start=start,
        end=end,
    )
    execution_id = fetcher.submit_query(
        agent_name=agent_name,
        metric_type=metric_type,
        start=start,
        end=end,
        limit=budget,
    )

    pages = 0
    trajectories = 0
    truncated = False
    page = fetcher.fetch_page(
        execution_id=execution_id, metric_type=metric_type
    )
    while True:
        if page.dataset.eval_cases:
            pages += 1
            trajectories += len(page.dataset.eval_cases)
            for evaluation in evaluations:
                evaluation.evaluate(state, page)
        truncated = truncated or page.memory_truncated
        if page.is_exhausted:
            break
        page = fetcher.fetch_page(
            execution_id=execution_id,
            metric_type=metric_type,
            page_token=page.next_page_token,
        )

    # Once for the sweep, not once per evaluation: the tallies belong to the
    # ingestion, which happened a single time however many evaluations read it.
    _common.add_ingestion_counts(state, fetcher)

    if not pages:
        if (
            scanned == 0
            and state.get("standalone")
            and not state.get("selector_sql")
        ):
            present = _common.count_by_agent(
                fetcher,
                node_name=node_name,
                metric_type=metric_type,
                start=start,
                end=end,
            )
            raise EmptyWindowError(
                render_empty_window_error(
                    state, present, agent_name, start, end
                )
            )
        logger.warning("%s: no trajectories found; skipping.", node_name)
        return [
            render_nothing_evaluated_line(
                state, scanned, agent_name, start, end
            )
        ]

    pages_key = build_pages_key(metric_type)
    state[pages_key] = int(state.get(pages_key, 0) or 0) + pages
    # Once, by the sweep, for every counter an analyzer contributes to. The
    # tallies belong to the ingestion, which happened a single time however
    # many analyzers read it, so an analyzer adding its own would report six
    # trajectories where three were ingested.
    outcomes = collapse_outcomes(
        evaluation.outcomes for evaluation in evaluations
    )
    add_counts(
        state,
        traces_evaluated=trajectories,
        traces_eval_passed=outcomes[PASSED],
        traces_eval_failed=outcomes[FAILED],
        traces_eval_errored=outcomes[ERRORED],
        findings_generated=sum(e.findings_count for e in evaluations),
        rubrics_generated=sum(e.rubrics_count for e in evaluations),
    )

    # Every evaluation finishes even when one of them fails, so a reviewer
    # outage cannot throw away the counters and findings a healthy code-metric
    # run beside it already produced. The first failure is raised afterwards,
    # which still fails the sweep.
    first_error: BaseException | None = None
    for evaluation in evaluations:
        try:
            evaluation.finish(state)
        except BaseException as exc:
            logger.exception(
                "%s: evaluation %r failed.", node_name, evaluation.name
            )
            first_error = first_error or exc
    if first_error is not None:
        raise first_error

    suffix = " (stopped early near the memory limit)" if truncated else ""
    return [evaluation.render_summary() + suffix for evaluation in evaluations]


def render_nothing_evaluated_line(
    state: ADKStateLike,
    scanned: int | None,
    agent_name: str,
    start: dt.datetime,
    end: dt.datetime,
) -> str:
    """Generate the summary explanation when a sweep evaluates zero cases.

    Handles three distinct cases:
    - Target selector matched zero sessions: notifies that the custom criteria
      found no matching conversations in the window.
    - Window contained zero traces: indicates an idle agent or a mismatched
      `AQA_OBSERVED_AGENT_NAME` configuration.
    - Otherwise: traces existed but none were ingested, or the trace count is
      unknown (`scanned` is None).

    Args:
        state: Execution state dictionary.
        scanned: Total count of records matching target criteria, or None.
        agent_name: Configured agent name evaluated during the sweep.
        start: Start timestamp of the evaluated window.
        end: End timestamp of the evaluated window.

    Returns:
        Formatted markdown message explaining why no evaluations occurred.
    """
    if scanned != 0:
        return "_Skipped (no trajectories found)._"
    if state.get("selector_sql", ""):
        return (
            "_Skipped: the selection matched no sessions._ Nothing met the "
            "criteria this investigation was scoped to; the run covered "
            f"{_format_window_bound(start)} to {_format_window_bound(end)}."
        )
    source = state.get("telemetry_ingestion_source") or "the telemetry source"
    return (
        "_Skipped: the window held no traces at all._ Nothing in "
        f"`{source}` matched agent `{agent_name}` between {_format_window_bound(start)} and "
        f"{_format_window_bound(end)}. Either the agent served no traffic in that window, or "
        "`AQA_OBSERVED_AGENT_NAME` is not the name its telemetry records it "
        "under."
    )


def render_empty_window_error(
    state: ADKStateLike,
    present: Mapping[str, int] | None,
    agent_name: str,
    start: dt.datetime,
    end: dt.datetime,
) -> str:
    """Explain a standalone sweep's empty window, naming what to change.

    Plain text rather than markdown, because it becomes the run's error.

    Args:
        state: Execution state dictionary.
        present: Trace counts per agent name the window does hold, from
            `BaseFetcher.count_by_agent`, or `None` when they could not be
            taken.
        agent_name: Configured agent name the sweep filtered on.
        start: Start timestamp of the evaluated window.
        end: End timestamp of the evaluated window.

    Returns:
        The error message.
    """
    source = state.get("telemetry_ingestion_source") or "the telemetry source"
    table = ".".join(
        part
        for part in (
            state.get("telemetry_dataset"),
            state.get("telemetry_table"),
        )
        if part
    )
    where = f"{source} ({table})" if table else source
    head = (
        f"No traces for agent '{agent_name}' in {where} between "
        f"{_format_window_bound(start)} and {_format_window_bound(end)}."
    )
    if present is None:
        return (
            f"{head} The agent names it does record could not be listed. Check "
            "that AQA_OBSERVED_AGENT_NAME is the name the agent's telemetry "
            "records, and that the agent served traffic in that window."
        )
    if not present:
        return (
            f"{head} No agent recorded any traces there. Either the agent served "
            "no traffic in that window, or the telemetry source, dataset and "
            "table are not where it exports to."
        )
    names = ", ".join(f"'{name}' ({count})" for name, count in present.items())
    busiest = (
        f" (the {len(present)} busiest)"
        if len(present) >= AGENT_COUNT_LIMIT
        else ""
    )
    return (
        f"{head} Agents with traces there{busiest}: {names}. Set "
        "AQA_OBSERVED_AGENT_NAME to the one to watch."
    )


def _format_window_bound(moment: dt.datetime) -> str:
    """A window bound, to the second: this line is read to check a setting."""
    return moment.replace(microsecond=0).isoformat()


def build_pages_key(metric_type: MetricType) -> str:
    """State key holding the page count the dashboard reads for this scope.

    Derived per scope so downstream breakdowns stay accurate. A shared key would
    attribute all pages to one scope and leave the other at zero, even though the
    sum remains correct. Incremented once per page, not once per evaluation, since
    it counts what was ingested rather than what read it.
    """
    return f"{metric_type.value.lower()}_pages_evaluated"


def collapse_outcomes(outcomes: Iterable[Mapping[str, str]]) -> dict[str, int]:
    """Count each trajectory once, whatever number of analyzers judged it.

    Takes each analyzer's `Evaluation.outcomes`. A trajectory several of them
    judged keeps the worst verdict, by the same `OUTCOME_RANK` that orders the
    metrics within one trajectory. Adding up each analyzer's own tally instead
    would report six verdicts over three trajectories the moment a second
    analyzer joined the sweep.

    The unit is one trajectory, which is what `traces_evaluated` counts: a whole
    session under ``MULTI_TURN`` and a single turn under ``SINGLE_TURN``.
    """
    worst: dict[str, str] = {}
    for per_analyzer in outcomes:
        for key, outcome in per_analyzer.items():
            if (
                key not in worst
                or OUTCOME_RANK[outcome] > OUTCOME_RANK[worst[key]]
            ):
                worst[key] = outcome
    counts = Counter(worst.values())
    # Every outcome, including the zeros: `run_sweep` writes all three counters.
    return {outcome: counts[outcome] for outcome in OUTCOME_RANK}
