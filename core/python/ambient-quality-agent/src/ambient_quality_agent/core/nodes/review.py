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

import contextvars
import hashlib
import logging
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
from ambient_quality_agent.tools.evaluation.results import build_outcome_keys
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
    from collections.abc import Mapping

    from ambient_quality_agent.tools.ingestion.models import Page

logger = logging.getLogger(__name__)

NODE_NAME = "review"


REVIEW_CONCURRENCY = 8
"""Maximum concurrent session review requests per page."""

_CaseResult = tuple[list[Finding], list[AgentRevision], Exception | None]
"""One review's findings, agent configurations, and error, if any."""


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
    evaluations: list[_sweep.Evaluation] = [
        ReviewEvaluation(_common.session_reviewer_factory(ctx), goal)
    ]
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

    lines = _sweep.run_sweep(
        ctx, evaluations, node_name=NODE_NAME, budget=budget
    )
    return WorkflowState.record_progress_event(
        state, "\n".join(lines) + warning + "\n", NODE_NAME
    )


class ReviewEvaluation:
    """Reviews each page with one session reviewer run per session."""

    name = "session_review"

    def __init__(
        self, reviewer: _common.SessionReviewFn, goal: InvestigationGoal
    ) -> None:
        self._reviewer = reviewer
        self._goal = goal
        # Cache parsed agent instructions and tools across the sweep.
        self._revision_cache = AgentRevisionCache()
        self._tally = ReviewStats()
        self.outcomes: dict[str, str] = {}

    def evaluate(self, state: ADKStateLike, page: Page) -> None:
        _review_page(
            state,
            page,
            self._revision_cache,
            self._reviewer,
            self._tally,
            outcomes=self.outcomes,
            goal=self._goal.text,
        )

    @property
    def findings_count(self) -> int:
        return self._tally.findings

    @property
    def rubrics_count(self) -> int:
        return 0

    def finish(self, state: ADKStateLike) -> None:
        tally = self._tally
        # Fail the sweep if every review errored and at least one error was
        # not an unparsable reply, which indicates an infrastructure outage.
        # An unparsable reply is the model's output failing, not the service,
        # so `first_error` never holds one.
        if tally.first_error is not None and tally.errored == tally.reviewed:
            raise tally.first_error

    def render_summary(self) -> str:
        line = (
            f"**Session review:** {self._tally.reviewed} session(s) reviewed, "
            f"{self._tally.findings} finding(s)."
        )
        if self._goal.version:
            return f"{line} Reviewed with goal version `{self._goal.version}`."
        if self._goal.source == "unreadable":
            return (
                f"{line} **The goal could not be read; reviewed without it.**"
            )
        return line


class ReviewStats:
    """Execution statistics accumulator for session review pages."""

    def __init__(self) -> None:
        self.pages = 0
        self.reviewed = 0
        self.passed = 0
        self.failed = 0
        self.errored = 0
        self.findings = 0
        self.first_error: Exception | None = None
        """First review exception other than an unparsable reply, re-raised if
        all reviews fail."""


def _review_page(
    state: ADKStateLike,
    page: Page,
    revision_cache: AgentRevisionCache,
    reviewer: _common.SessionReviewFn,
    tally: ReviewStats,
    *,
    outcomes: dict[str, str],
    goal: str,
) -> None:
    """Review turns_by_trajectory in a page and append resulting FindingSet to state.

    The page carries the deployment revision each session ran on, which keys the
    agent-configuration cache and attributes findings to release builds.

    Args:
        state: Workflow execution state.
        page: Ingested telemetry page.
        revision_cache: Cache for agent instructions and tool configurations.
        reviewer: Session reviewer run for every session.
        tally: Statistics tracker for review outcomes.
        outcomes: Dictionary collecting per-case outcome statuses.
        goal: The developer's goal every session is reviewed with; empty for none.
    """
    eval_cases = page.dataset.eval_cases or []
    revisions = page.revisions
    focus = str(state.get("session_review_focus", "") or "")
    page_index = tally.pages
    logger.info(
        "%s page %d: reviewing %d session(s)",
        NODE_NAME,
        page_index,
        len(eval_cases),
    )
    with futures.ThreadPoolExecutor(max_workers=REVIEW_CONCURRENCY) as pool:
        # Each review runs in its own copy of this thread's context, so its
        # telemetry spans are children of the node's span and its log records
        # carry the investigation ID. A context cannot be entered by two
        # threads at once, so every review gets a fresh copy.
        submitted = [
            pool.submit(
                contextvars.copy_context().run,
                _review_case,
                case,
                revisions,
                revision_cache,
                reviewer,
                session_review_focus=focus,
                goal=goal,
            )
            for case in eval_cases
        ]
        # ty cannot infer the return type through `Context.run`.
        results = [cast("_CaseResult", future.result()) for future in submitted]

    findings: list[Finding] = []
    turns_by_trajectory: dict[str, list[dict]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    keys = build_outcome_keys(eval_cases, tally.reviewed)
    # Preserve deterministic page order when aggregating findings across threads.
    for position, (eval_case, (found, case_revisions, error)) in enumerate(
        zip(eval_cases, results, strict=True)
    ):
        tally.reviewed += 1
        trajectory_id = str(getattr(eval_case, "eval_case_id", "") or "")
        outcome_key = keys[position]
        if error is not None:
            tally.errored += 1
            outcomes[outcome_key] = "errored"
            if tally.first_error is None and not isinstance(
                error, agentic_reviewer.UnparsableReplyError
            ):
                tally.first_error = error
            continue
        if not found:
            tally.passed += 1
            outcomes[outcome_key] = "passed"
            continue
        tally.failed += 1
        outcomes[outcome_key] = "failed"
        revision = revisions.get(trajectory_id, "")
        findings.extend(
            finding.model_copy(update={"agent_revision": revision})
            if revision
            else finding
            for finding in found
        )
        if trajectory_id and trajectory_id not in turns_by_trajectory:
            turns_by_trajectory[trajectory_id] = extract_trajectory_turns(
                eval_case
            )
            if case_revisions:
                agent_revisions[trajectory_id] = case_revisions

    tally.findings += len(findings)
    tally.pages += 1
    # Reassign finding_sets list to trigger ADK state delta change tracking.
    state["finding_sets"] = [
        *(state.get("finding_sets") or []),
        FindingSet(
            findings=findings,
            turns_by_trajectory=turns_by_trajectory,
            agent_revisions=agent_revisions,
        ).model_dump(),
    ]
    logger.info(
        "%s page %d stored: %d finding(s)", NODE_NAME, page_index, len(findings)
    )


def _review_case(
    eval_case: Any,
    revisions: Mapping[str, str],
    revision_cache: AgentRevisionCache,
    reviewer: _common.SessionReviewFn,
    *,
    session_review_focus: str,
    goal: str,
) -> _CaseResult:
    """Review one session, returning findings, configurations, and any error.

    Errors are returned rather than raised to isolate individual failures.

    Args:
        eval_case: Individual evaluation case representing a conversation.
        revisions: Mapping of session IDs to agent revision strings.
        revision_cache: Cache for parsed agent configurations.
        reviewer: Session reviewer run for the session.
        session_review_focus: Review focus prompt instructions.
        goal: The developer's goal to review with; empty for none.

    Returns:
        Tuple of (extracted findings, agent revisions, caught exception or None).
    """
    trajectory_id = str(getattr(eval_case, "eval_case_id", "") or "")
    try:
        case_revisions = revision_cache.build_revisions(
            eval_case, revisions.get(trajectory_id, "")
        )
        return (
            reviewer(
                eval_case,
                case_revisions,
                session_review_focus=session_review_focus,
                goal=goal,
            ),
            case_revisions,
            None,
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
        return [], [], exc
