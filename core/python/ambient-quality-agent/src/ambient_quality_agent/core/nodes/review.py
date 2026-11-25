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

"""The ``review`` workflow node: the ``session_review`` producer.

Ingests the window once and hands every page to each enabled evaluation, which
produce the `FindingSet` payloads `insight_correlation` consumes. The LLM
reviewer is one of those evaluations and always runs. Custom metrics -- Python
or judged -- are additive rather than a mode of their own, so publishing a
library turns them on and publishing none leaves the reviewer running alone.
"""

from __future__ import annotations

import collections
import contextvars
import dataclasses
import hashlib
import logging
import traceback
from concurrent import futures
from typing import TYPE_CHECKING, Any, cast

from ambient_quality_agent.core._logging import log_node_run
from ambient_quality_agent.core.nodes import (
    _code_metrics,
    _common,
    _remote_code_metrics,
    _sweep,
)
from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    AgentRevisionCache,
)
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
from ambient_quality_agent.tools.evaluation.results import (
    ERRORED,
    FAILED,
    PASSED,
    build_outcome_keys,
)
from ambient_quality_agent.tools.insights.findings import (
    Finding,
    FindingSet,
    extract_trajectory_turns,
)
from ambient_quality_agent.tools.review import agentic_reviewer
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ambient_quality_agent.tools.ingestion.models import Page

logger = logging.getLogger(__name__)

NODE_NAME = "review"


REVIEW_CONCURRENCY = 8
"""Maximum session reviews that run at once across the sweep."""


@dataclasses.dataclass(frozen=True, slots=True)
class _CaseFailure:
    """Findings and supporting context from one session that failed review."""

    trajectory_id: str
    findings: list[Finding]
    turns: list[dict[str, Any]]
    agent_revisions: list[AgentRevision]


_CaseResult = tuple[_CaseFailure | None, Exception | None]
"""One review's failure payload (when it filed findings) and its error, if any."""


@node(name=NODE_NAME)
@log_node_run(NODE_NAME)
def review_node(ctx: Context) -> Event:
    """Review all sessions in the time window and record a `FindingSet` per page.

    Args:
        ctx: Workflow execution context.

    Returns:
        ADK Event containing review progress summary.
    """
    state = ctx.state
    budget = int(state.get("budget_per_metric", 0) or 0)
    if budget <= 0:
        logger.warning("%s: zero budget; skipping.", NODE_NAME)
        return WorkflowState.record_progress_event(
            state,
            "**Session review:** _Skipped (zero budget)._\n",
            NODE_NAME,
        )

    # Read once, so every session in the investigation is reviewed with the same goal.
    goal = _common.goal_loader(ctx)
    section = agentic_reviewer.render_goal_section(goal.text)
    state["goal"] = {
        "version": goal.version,
        "source": goal.source,
        "prompt_sha256": (
            hashlib.sha256(section.encode("utf-8")).hexdigest()
            if section
            else None
        ),
    }

    # Custom metrics (Python or judged) run alongside the review: configured
    # metrics are evaluated on ingested pages, while an empty or failed library
    # leaves review running alone.
    #
    # TODO: Decouple custom metrics into an independent producer
    # node once shared ingestion caching is available.
    progress = _sweep.SweepProgress(state, "Session review")
    review_evaluation = ReviewEvaluation(
        _common.session_reviewer_factory(ctx),
        goal,
        on_reviewed=progress.advance,
    )
    evaluations: list[_sweep.Evaluation] = [review_evaluation]
    library, warning = _common.metric_library_factory(ctx)
    # Register evaluation pass even with only declined or refused metrics so finish()
    # records them in metrics_detail.
    if library or library.declined or library.failures:
        evaluations.append(_code_metrics.CodeMetricEvaluation(library))
    # Check remote and judged metrics separately because a configuration made
    # only of them loads an empty local library mapping. Both go to the eval
    # service in one call, so they share one evaluation.
    remote = [*library.remote, *library.judged]
    if remote:
        evaluations.append(
            _remote_code_metrics.RemoteCodeMetricEvaluation(
                lambda: _common.evaluator_factory(ctx), remote
            )
        )

    try:
        lines = _sweep.run_sweep(
            ctx,
            evaluations,
            node_name=NODE_NAME,
            budget=budget,
            progress=progress,
        )
    finally:
        review_evaluation.close()
    return WorkflowState.record_progress_event(
        state, "\n".join(lines) + warning + "\n", NODE_NAME
    )


class ReviewEvaluation:
    """Reviews sessions across pages on one shared pool."""

    name = "session_review"

    def __init__(
        self,
        reviewer: _common.SessionReviewFn,
        goal: InvestigationGoal,
        *,
        on_reviewed: Callable[[int, int], None] = lambda *_: None,
    ) -> None:
        """Sets up the review of a sweep's pages.

        Args:
            reviewer: Reviewer model callable.
            goal: The developer's goal every session is reviewed with.
            on_reviewed: Called on the node thread as sessions finish inside an
                `evaluate` or `finish` call, with the sessions reviewed and
                findings filed during that call (`SweepProgress.advance`).
        """
        self._reviewer = reviewer
        self._goal = goal
        self._on_reviewed = on_reviewed
        # Cache parsed agent instructions and tools across the sweep.
        self._revision_cache = AgentRevisionCache()
        self.outcomes: dict[str, str] = {}
        self._pool: futures.ThreadPoolExecutor | None = None
        self._submitted = 0
        self._reviewed = 0
        self._errored = 0
        self._findings = 0
        self._first_error: Exception | None = None
        self._first_error_order: int | None = None
        self._next_page_index = 0
        self._pages: collections.deque[_PageRecord] = collections.deque()
        self._in_flight: dict[
            futures.Future[Any], tuple[_PageRecord, int, int, str]
        ] = {}

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        """Submit `page`'s sessions to the review pool, bounded by the review window.

        Args:
            state: Workflow execution state.
            page: Ingested telemetry page.
        """
        eval_cases = page.dataset.eval_cases or []
        revisions = page.revisions
        focus = str(state.get("session_review_focus", "") or "")
        finding_sets = list(state.get("finding_sets") or [])
        finding_set_index = len(finding_sets)
        # Reserve this page's position before peer evaluations append theirs,
        # reassigning the list so ADK tracks the state change.
        state["finding_sets"] = [*finding_sets, FindingSet().model_dump()]
        page_index = self._next_page_index
        self._next_page_index += 1
        outcome_keys = build_outcome_keys(eval_cases, self._submitted)
        base_order = self._submitted
        self._submitted += len(eval_cases)
        record = _PageRecord(
            page_index=page_index,
            finding_set_index=finding_set_index,
            failures=[None] * len(eval_cases),
            remaining=len(eval_cases),
        )
        self._pages.append(record)
        logger.info(
            "%s page %d: reviewing %d session(s)",
            NODE_NAME,
            page_index,
            len(eval_cases),
        )
        reviewed_in_call = 0
        filed_in_call = 0
        if eval_cases:
            concurrency = max(1, REVIEW_CONCURRENCY)
            # Half run on the pool and half wait in its queue, so workers stay
            # fed while the sweep fetches the next page.
            window = 2 * concurrency
            if self._pool is None:
                self._pool = futures.ThreadPoolExecutor(max_workers=concurrency)
            pool = self._pool
            for case_index, (case, outcome_key) in enumerate(
                zip(eval_cases, outcome_keys, strict=True)
            ):
                while len(self._in_flight) >= window:
                    done, _ = futures.wait(
                        self._in_flight, return_when=futures.FIRST_COMPLETED
                    )
                    reviewed_in_call, filed_in_call = self._collect_reviews(
                        done, reviewed_in_call, filed_in_call
                    )
                trajectory_id = str(getattr(case, "eval_case_id", "") or "")
                # Each review runs in its own copy of this thread's context, so its
                # telemetry spans are children of the node's span and its log
                # records carry the investigation ID. A context cannot be entered
                # by two threads at once, so every review gets a fresh copy.
                submitted = pool.submit(
                    contextvars.copy_context().run,
                    _review_case,
                    # In a list that the review empties: while an error raised in
                    # a review is kept, so are the arguments the pool called it
                    # with.
                    [case],
                    trajectory_id,
                    revisions.get(trajectory_id, ""),
                    self._revision_cache,
                    self._reviewer,
                    session_review_focus=focus,
                    goal=self._goal.text,
                )
                self._in_flight[submitted] = (
                    record,
                    case_index,
                    base_order + case_index,
                    outcome_key,
                )
        while done_now := [f for f in self._in_flight if f.done()]:
            reviewed_in_call, filed_in_call = self._collect_reviews(
                done_now, reviewed_in_call, filed_in_call
            )
        self._record_ready_pages(state)

    @property
    def findings_count(self) -> int:
        return self._findings

    @property
    def rubrics_count(self) -> int:
        return 0

    @property
    def pending_count(self) -> int:
        return len(self._in_flight)

    def finish(self, state: ADKStateLike) -> None:
        """Wait for remaining reviews, record pages in order, and fail on an outage.

        Args:
            state: Workflow execution state.

        Raises:
            Exception: Re-raises the first non-parse review error when every
                reviewed session errored.
        """
        try:
            self._collect_reviews(futures.as_completed(list(self._in_flight)))
            self._record_ready_pages(state)
        finally:
            self.close()
        # Fail the sweep if every review errored and at least one error was
        # not an unparsable reply, which indicates an infrastructure outage.
        # An unparsable reply is the model's output failing, not the service,
        # so `_first_error` never holds one.
        if self._first_error is not None and self._errored == self._reviewed:
            raise self._first_error

    def close(self) -> None:
        """Cancel queued reviews and shut down the worker pool."""
        pool, self._pool = self._pool, None
        self._in_flight.clear()
        self._pages.clear()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def _collect_reviews(
        self,
        completed: Iterable[futures.Future[Any]],
        reviewed_in_call: int = 0,
        filed_in_call: int = 0,
    ) -> tuple[int, int]:
        """Collect completed review futures and report incremental progress.

        Args:
            completed: Completed futures from `_in_flight`.
            reviewed_in_call: Sessions collected earlier in the current call.
            filed_in_call: Findings collected earlier in the current call.

        Returns:
            Updated `(reviewed_in_call, filed_in_call)` counts.
        """
        for future in completed:
            filed_in_call += self._collect_review(future)
            reviewed_in_call += 1
            self._on_reviewed(reviewed_in_call, filed_in_call)
        return reviewed_in_call, filed_in_call

    def _collect_review(self, completed: futures.Future[Any]) -> int:
        """Record one completed review's verdict, failure payload, and error.

        Args:
            completed: Completed future from `_in_flight`.

        Returns:
            Number of findings filed by the completed review.
        """
        record, case_index, case_order, outcome_key = self._in_flight.pop(
            completed
        )
        # ty cannot infer the return type through `Context.run`.
        failure, error = cast("_CaseResult", completed.result())
        self._reviewed += 1
        record.remaining -= 1
        if error is not None:
            _clear_frame_locals(error)
            self._errored += 1
            self.outcomes[outcome_key] = ERRORED
            if not isinstance(
                error, agentic_reviewer.UnparsableReplyError
            ) and (
                self._first_error_order is None
                or case_order < self._first_error_order
            ):
                self._first_error_order = case_order
                self._first_error = error
            return 0
        if failure is None:
            self.outcomes[outcome_key] = PASSED
            return 0
        self.outcomes[outcome_key] = FAILED
        record.failures[case_index] = failure
        filed = len(failure.findings)
        self._findings += filed
        return filed

    def _record_ready_pages(self, state: ADKStateLike) -> None:
        """Record and drop leading pages whose reviews have all completed.

        Args:
            state: Workflow execution state.
        """
        while self._pages and self._pages[0].remaining == 0:
            _record_page(state, self._pages.popleft())

    def render_summary(self) -> str:
        """Render the markdown progress summary line for session review.

        Returns:
            Formatted summary line including any goal version or warning.
        """
        line = (
            f"**Session review:** {self._reviewed} session(s) reviewed, "
            f"{self._findings} finding(s)."
        )
        if self._goal.version:
            return f"{line} Reviewed with goal version `{self._goal.version}`."
        if self._goal.source == "unreadable":
            return (
                f"{line} **The goal could not be read; reviewed without it.**"
            )
        return line


@dataclasses.dataclass
class _PageRecord:
    """What recording a page needs once its reviews finish.

    Holds none of the page's sessions or review exceptions.
    """

    page_index: int
    """Zero-based page number within this sweep."""

    finding_set_index: int
    """Reserved index of this page's `FindingSet` in `state["finding_sets"]`."""

    failures: list[_CaseFailure | None]
    """Failed reviews in page order; `None` for passed, errored, or pending sessions."""

    remaining: int
    """Uncollected reviews remaining on this page."""


def _clear_frame_locals(error: BaseException) -> None:
    """Clear the local variables held by the frames of an error's tracebacks.

    A traceback keeps the frames the error passed through, and their local
    variables include the session under review and the prompt built from it.
    The tracebacks stay printable. Call it once the review has returned: a
    frame that is still running is not cleared.

    Args:
        error: A failed review's error. The errors it was raised from or
            during are cleared too.
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        pending += [current.__cause__, current.__context__]
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)


def _record_page(state: ADKStateLike, record: _PageRecord) -> None:
    """Write a completed page's `FindingSet` into its reserved state slot.

    Takes none of the page's sessions: `record` carries only failed reviews.

    Args:
        state: Workflow execution state.
        record: The page's reserved slot and failed review payloads.
    """
    findings: list[Finding] = []
    turns_by_trajectory: dict[str, list[dict[str, Any]]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    # Preserve deterministic page order when aggregating findings across threads.
    for failure in record.failures:
        if failure is None:
            continue
        findings.extend(failure.findings)
        trajectory_id = failure.trajectory_id
        if trajectory_id and trajectory_id not in turns_by_trajectory:
            turns_by_trajectory[trajectory_id] = failure.turns
            if failure.agent_revisions:
                agent_revisions[trajectory_id] = failure.agent_revisions

    dumped = FindingSet(
        findings=findings,
        turns_by_trajectory=turns_by_trajectory,
        agent_revisions=agent_revisions,
    ).model_dump()
    # Reassign finding_sets list to trigger ADK state delta change tracking.
    finding_sets = list(state.get("finding_sets") or [])
    finding_sets[record.finding_set_index] = dumped
    state["finding_sets"] = finding_sets
    logger.info(
        "%s page %d stored: %d finding(s)",
        NODE_NAME,
        record.page_index,
        len(findings),
    )


def _review_case(
    case_holder: list[Any],
    trajectory_id: str,
    revision: str,
    revision_cache: AgentRevisionCache,
    reviewer: _common.SessionReviewFn,
    *,
    session_review_focus: str,
    goal: str,
) -> _CaseResult:
    """Review one session, returning any failure payload or caught error.

    Errors are returned rather than raised to isolate individual failures.

    Args:
        case_holder: One-item list holding the evaluation case, a conversation.
            Emptied here, so the caller's arguments do not keep the case.
        trajectory_id: Session ID, or empty string when the session has none.
        revision: Deployment revision string for this session, or empty.
        revision_cache: Cache for parsed agent configurations.
        reviewer: Session reviewer run for the session.
        session_review_focus: Review focus prompt instructions.
        goal: The developer's goal to review with; empty for none.

    Returns:
        Tuple of (`_CaseFailure` when findings were extracted else `None`,
        caught exception or `None`).
    """
    eval_case = case_holder.pop()
    try:
        case_revisions = revision_cache.build_revisions(eval_case, revision)
        found = reviewer(
            eval_case,
            case_revisions,
            session_review_focus=session_review_focus,
            goal=goal,
        )
    except Exception as exc:
        logger.warning(
            "%s: reviewing session %s failed: %s",
            NODE_NAME,
            getattr(eval_case, "eval_case_id", "<unknown>"),
            exc,
            # An unparsable reply is an expected model outcome, and the
            # reviewer already logs it.
            exc_info=not isinstance(exc, agentic_reviewer.UnparsableReplyError),
        )
        return None, exc
    if not found:
        return None, None
    if revision:
        found = [
            finding.model_copy(update={"agent_revision": revision})
            for finding in found
        ]
    # Serialized by the review, so that recording the page needs no session.
    turns = extract_trajectory_turns(eval_case) if trajectory_id else []
    kept_revisions = case_revisions if trajectory_id else []
    return (
        _CaseFailure(
            trajectory_id=trajectory_id,
            findings=found,
            turns=turns,
            agent_revisions=kept_revisions,
        ),
        None,
    )
