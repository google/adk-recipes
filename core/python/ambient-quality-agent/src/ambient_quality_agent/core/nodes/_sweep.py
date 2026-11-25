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
        │ SweepProgress ──► _stage.StageReporter
        │   once before the first page, after each page, once every
        │   evaluation finishes, while one reports, and kept alive between
        ▼
    _common.add_ingestion_counts()
        │ compute_trajectories_represented() ◄── Page.sampling
        ▼
    state trajectories_represented
        │
        ├── no pages, deployed ──► render_nothing_evaluated_line()
        ├── no traces, standalone ──► _common.count_by_agent()
        │                             ──► raise EmptyWindowError
        ▼
    Evaluation.finish() ──► every Evaluation (Protocol) in the list
        │ SweepProgress.keep_alive()
        ▼
    resolve_worst_outcomes() ◄── Evaluation.outcomes
        │ collapse_outcomes(), build_pages_key()
        │ compute_estimated_counts()
        ▼
    add_counts() ──► state counters
        │
        ▼
    Evaluation.render_summary() ──► summary lines

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

from ambient_quality_agent.core.nodes import _common, _stage
from ambient_quality_agent.core.state import add_counts
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    OUTCOME_RANK,
    PASSED,
)
from ambient_quality_agent.tools.ingestion.base import AGENT_COUNT_LIMIT
from ambient_quality_agent.tools.investigations.models import StageProgress

if TYPE_CHECKING:
    import contextlib
    from collections.abc import Iterable, Mapping

    from ambient_quality_agent.core.state import ADKStateLike
    from ambient_quality_agent.tools.ingestion.models import (
        Page,
        SamplingRecord,
    )
    from google.adk.agents.context import Context

logger = logging.getLogger(__name__)

TRAJECTORIES_REPRESENTED_KEY = "trajectories_represented"
"""State key holding how many trajectories of all traffic each evaluated one
stands for, keyed by trajectory id (`WorkflowState.trajectories_represented`)."""


class EmptyWindowError(RuntimeError):
    """A standalone sweep found no traces at all for the observed agent.

    Deployed, an empty window is a warning: AQuA cannot know the agent's traffic
    pattern, and an idle hour must not fail a scheduled investigation. A standalone run is
    started by hand to see what AQuA finds, so a window with nothing in it is
    almost always a setting to fix, and failing names the setting.
    """


class Evaluation(Protocol):
    """One way of judging a page of trajectories.

    Implementations append their findings to `state["finding_sets"]`, which is
    the single shape `insight_correlation` consumes, so adding an evaluation
    needs no change downstream. The sweep totals `outcomes`, `findings_count`,
    and `rubrics_count` into state counters after `finish` returns, so an
    evaluation that runs across pages can complete its remaining work in
    `finish`.
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

    @property
    def pending_count(self) -> int:
        """Trajectories handed to `evaluate` that this evaluation has not judged yet."""
        ...

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        """Judge one page and record what it found."""

    def render_summary(self) -> str:
        """One markdown line for the investigation's progress event."""

    def finish(self, state: ADKStateLike) -> None:
        """Complete remaining work, finalize state, and raise if the evaluation failed outright.

        Args:
            state: Workflow state to finalize.

        Raises:
            Exception: If the evaluation failed outright.
        """


class SweepProgress:
    """How far a sweep got, written as the investigation's stage rows.

    `run_sweep` writes one before the first page, one after every page for the
    trajectories no evaluation still owes, and one once every evaluation has
    finished, keeping the newest alive while evaluations run or finish. A window
    usually arrives as a single page, so those alone would jump from none to
    all: an evaluation that judges a page one trajectory at a time reports
    through `advance` as it goes, and those reports are written at most once
    every `_stage.PROGRESS_INTERVAL_SECONDS`.
    """

    def __init__(self, state: ADKStateLike, stage: str) -> None:
        """Binds the rows to an investigation and names the stage they report.

        Args:
            state: Workflow state carrying the ``investigation_id`` to write
                under.
            stage: Name of the sweep in its rows, such as "Session review".
        """
        self._reporter = _stage.StageReporter(state)
        self._stage = stage
        self._total = 0
        self._done = 0
        self._findings = 0

    def start(self, total: int) -> None:
        """Writes the first row: nothing judged yet, out of `total`.

        Args:
            total: Trajectories the sweep expects to judge.
        """
        self._total = total
        self._reporter.write(self._build_progress(0, 0))

    def advance(self, judged: int, findings: int) -> None:
        """Reports trajectories judged since the last `finish_page`, if a row is due.

        Args:
            judged: Trajectories judged since the last `finish_page` (or `start`).
            findings: Findings filed since the last `finish_page` (or `start`).
        """
        self._reporter.write_if_due(
            self._build_progress(self._done + judged, self._findings + findings)
        )

    def finish_page(self, trajectories: int, findings: int) -> None:
        """Writes the row for the trajectories every evaluation has finished.

        Args:
            trajectories: Trajectories judged by every evaluation so far.
            findings: Findings filed so far.
        """
        self._done, self._findings = trajectories, findings
        progress = self._build_progress(trajectories, findings)
        # The row from inside the page may say the same; liveness is
        # `keep_alive`'s job, so an unchanged count costs no load job.
        if progress != self._reporter.latest:
            self._reporter.write(progress)

    def keep_alive(self) -> contextlib.AbstractContextManager[None]:
        """Re-writes the newest report while evaluations run or finish; see
        `_stage.StageReporter.keep_alive`.

        Returns:
            A context manager that keeps the row alive while its block runs.
        """
        return self._reporter.keep_alive()

    def _build_progress(self, done: int, findings: int) -> StageProgress:
        return StageProgress(
            stage=self._stage,
            evaluated=done,
            total=self._total,
            findings=findings,
        )


def run_sweep(
    ctx: Context,
    evaluations: list[Evaluation],
    *,
    node_name: str,
    budget: int,
    metric_type: MetricType = MetricType.MULTI_TURN,
    progress: SweepProgress | None = None,
) -> list[str]:
    """Page the telemetry window once and pass each page to every active evaluation.

    Args:
        ctx: Workflow context supplying state and configuration.
        evaluations: Evaluation passes to run over each ingested page.
        node_name: Node name for logging and error reporting.
        budget: Maximum number of sessions or traces to ingest.
        metric_type: Ingestion scope (multi-turn sessions or single-turn traces).
        progress: Where the sweep reports how far it got; when omitted, one
            that names the stage after `node_name`.

    Returns:
        Summary lines from each evaluation for progress reporting.

    Raises:
        selector.SelectorRefused: If the investigation carries an invalid or unauthorized
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
        sampling_seed=state.get("sampling_seed") or None,
    )

    if progress is None:
        progress = SweepProgress(state, node_name)
    # The window can hold fewer than the budget; when it could not be
    # counted, the budget is the most the sweep reads.
    progress.start(budget if scanned is None else min(budget, scanned))

    pages = 0
    trajectories = 0
    truncated = False
    samplings: dict[str, SamplingRecord] = {}
    unestimated = 0
    page = fetcher.fetch_page(
        execution_id=execution_id, metric_type=metric_type
    )
    while True:
        if page.dataset.eval_cases:
            pages += 1
            trajectories += len(page.dataset.eval_cases)
            for case in page.dataset.eval_cases:
                record = page.sampling.get(case.eval_case_id or "")
                if record is None:
                    unestimated += 1
                else:
                    samplings[str(case.eval_case_id)] = record
            # The eval service judges a page in one call, and a custom metric
            # may run long after the review: neither reports as it goes.
            with progress.keep_alive():
                for evaluation in evaluations:
                    evaluation.evaluate(state, page)
            progress.finish_page(
                trajectories
                - max((e.pending_count for e in evaluations), default=0),
                sum(e.findings_count for e in evaluations),
            )
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
    represented = compute_trajectories_represented(samplings, scanned)
    # Reassigned rather than updated, so ADK records the state change.
    state[TRAJECTORIES_REPRESENTED_KEY] = {
        **(state.get(TRAJECTORIES_REPRESENTED_KEY) or {}),
        **represented,
    }

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

    # Every evaluation finishes before the sweep totals their outcomes, under
    # `keep_alive` because an evaluation that runs across pages waits for its
    # remaining work here. Every evaluation finishes even when one fails, so a
    # reviewer outage cannot throw away the counters and findings a healthy
    # code-metric run beside it produced. The first failure is raised after the
    # counters are written, which still fails the sweep.
    first_error: BaseException | None = None
    with progress.keep_alive():
        for evaluation in evaluations:
            try:
                evaluation.finish(state)
            except _stage.InvestigationCancelledError:
                raise
            except BaseException as exc:
                logger.exception(
                    "%s: evaluation %r failed.", node_name, evaluation.name
                )
                first_error = first_error or exc

    pages_key = build_pages_key(metric_type)
    state[pages_key] = int(state.get(pages_key, 0) or 0) + pages
    # Estimates count one unit: an investigation that reviews both whole
    # conversations and single turns records none, as adding them would count
    # neither population.
    reviews_both_units = state.get(
        "quality_analysis_mode"
    ) == "eval_service" and bool(state.get("single_turn_metrics"))
    # Once, by the sweep, for every counter an analyzer contributes to. The
    # tallies belong to the ingestion, which happened a single time however
    # many analyzers read it, so an analyzer adding its own would report six
    # trajectories where three were ingested.
    worst = resolve_worst_outcomes(
        evaluation.outcomes for evaluation in evaluations
    )
    outcomes = collapse_outcomes([worst])
    add_counts(
        state,
        trajectories_evaluated=trajectories,
        trajectories_eval_passed=outcomes[PASSED],
        trajectories_eval_failed=outcomes[FAILED],
        trajectories_eval_errored=outcomes[ERRORED],
        findings_generated=sum(e.findings_count for e in evaluations),
        rubrics_generated=sum(e.rubrics_count for e in evaluations),
    )
    if unestimated:
        logger.warning(
            "%s: %d evaluated trajectory(s) carry no sampling probability; "
            "recording no estimates for all traffic.",
            node_name,
            unestimated,
        )
    elif reviews_both_units:
        logger.info(
            "%s: the investigation reviews both conversations and single "
            "turns; recording no estimates for all traffic.",
            node_name,
        )
    add_counts(
        state,
        **compute_estimated_counts(
            worst,
            samplings,
            represented,
            complete=unestimated == 0 and not reviews_both_units,
        ),
    )
    if first_error is not None:
        raise first_error

    progress.finish_page(
        trajectories, sum(e.findings_count for e in evaluations)
    )

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
    - Target selector matched zero sessions: notifies that the selector's
      criteria found no matching conversations in the window.
    - Window contained zero traces: indicates an idle agent or an agent
      attached under a name its telemetry does not record.
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
        "it is attached under a name its telemetry does not record; "
        "`agents-cli aqua attach` sets the name."
    )


def render_empty_window_error(
    state: ADKStateLike,
    present: Mapping[str, int] | None,
    agent_name: str,
    start: dt.datetime,
    end: dt.datetime,
) -> str:
    """Explain a standalone sweep's empty window, naming what to change.

    Plain text rather than markdown, because it becomes the investigation's error.

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
            "that the agent is attached under the name its telemetry records, "
            "and that the agent served traffic in that window."
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
        f"{head} Agents with traces there{busiest}: {names}. Attach the one "
        "to watch with `agents-cli aqua attach <agent name>`."
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


def resolve_worst_outcomes(
    outcomes: Iterable[Mapping[str, str]],
) -> dict[str, str]:
    """Picks one verdict per trajectory, whatever number of analyzers judged it.

    A trajectory several analyzers judged keeps the worst verdict, by the same
    `OUTCOME_RANK` that orders the metrics within one trajectory.

    Args:
        outcomes: Each analyzer's `Evaluation.outcomes`.

    Returns:
        The worst verdict of each trajectory, keyed as the analyzers key it.
    """
    worst: dict[str, str] = {}
    for per_analyzer in outcomes:
        for key, outcome in per_analyzer.items():
            if (
                key not in worst
                or OUTCOME_RANK[outcome] > OUTCOME_RANK[worst[key]]
            ):
                worst[key] = outcome
    return worst


def collapse_outcomes(outcomes: Iterable[Mapping[str, str]]) -> dict[str, int]:
    """Count each trajectory once, whatever number of analyzers judged it.

    Takes each analyzer's `Evaluation.outcomes`. A trajectory several of them
    judged keeps the worst verdict (`resolve_worst_outcomes`). Adding up each
    analyzer's own tally instead would report six verdicts over three
    trajectories the moment a second analyzer joined the sweep.

    The unit is one trajectory, which is what `trajectories_evaluated` counts: a whole
    session under ``MULTI_TURN`` and a single turn under ``SINGLE_TURN``.

    Args:
        outcomes: Each analyzer's verdict per trajectory, keyed by trajectory.

    Returns:
        The number of trajectories with each outcome in `OUTCOME_RANK`,
        including those with none.
    """
    counts = Counter(resolve_worst_outcomes(outcomes).values())
    # Every outcome, including the zeros: `run_sweep` writes all three counters.
    return {outcome: counts[outcome] for outcome in OUTCOME_RANK}


def compute_trajectories_represented(
    samplings: Mapping[str, SamplingRecord], scanned: int | None
) -> dict[str, float]:
    """Computes how many trajectories of all traffic each evaluated one stands for.

    Each counts as ``1 / p`` trajectories, where ``p`` is its probability of
    being picked. Summed over the sample, that estimates the candidates
    without bias, but with noise from a weighted sample's random threshold,
    and short by the picks that could not be read. Scaling every count so
    that the sum equals the candidates the window held removes both, and
    keeps each trajectory's share of the sum.

    Args:
        samplings: How each evaluated trajectory was picked, keyed by
            ``eval_case_id``.
        scanned: Candidates the window held, from `count_scanned`; None or 0
            when unknown, which leaves the counts unscaled.

    Returns:
        The trajectories each one stands for, keyed like `samplings`.
    """
    total = sum(
        1 / record.inclusion_probability for record in samplings.values()
    )
    scale = scanned / total if scanned and total else 1.0
    return {
        key: scale / record.inclusion_probability
        for key, record in samplings.items()
    }


def compute_estimated_counts(
    worst: Mapping[str, str],
    samplings: Mapping[str, SamplingRecord],
    represented: Mapping[str, float],
    *,
    complete: bool,
) -> dict[str, int]:
    """Estimates the sweep's trajectory counts for all traffic in its window.

    Each evaluated trajectory counts as the trajectories it stands for.
    Rounded to whole trajectories, as every counter is an integer.

    Args:
        worst: Each evaluated trajectory's verdict, from
            `resolve_worst_outcomes`.
        samplings: How each evaluated trajectory was picked, keyed by
            ``eval_case_id``.
        represented: The trajectories each one in `samplings` stands for,
            from `compute_trajectories_represented`.
        complete: Whether to record estimates: every evaluated trajectory
            is in `samplings`, and the investigation reviews one unit. An
            estimate missing some trajectories would read as a smaller
            window, and one adding sessions to turns as a larger one.

    Returns:
        Counter increments: ``trajectories_evaluated_with_risk_signals``, and
        when `complete`, the two ``estimated_*`` counters.
    """
    counts = {
        "trajectories_evaluated_with_risk_signals": sum(
            1
            for record in samplings.values()
            if record.weight is not None and record.weight > 1
        )
    }
    if not complete:
        return counts
    for outcome, name in (
        (PASSED, "estimated_trajectories_eval_passed"),
        (FAILED, "estimated_trajectories_eval_failed"),
    ):
        counts[name] = round(
            sum(
                represented[key]
                for key, verdict in worst.items()
                if verdict == outcome and key in represented
            )
        )
    return counts
