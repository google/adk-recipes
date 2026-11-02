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

import hashlib
import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

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
from ambient_quality_agent.tools.insights.findings import Finding, FindingSet
from ambient_quality_agent.tools.review import session_review
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ambient_quality_agent.tools.ingestion.models import Page

logger = logging.getLogger(__name__)

NODE_NAME = "review"


REVIEW_CONCURRENCY = 8
"""Maximum concurrent session review requests per page."""


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

    # Read once, so every session in the run is reviewed with the same goal.
    goal = _common.goal_loader(ctx)
    section = session_review.render_goal_section(goal.text)
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
    # TODO(b/558291983): Decouple custom metrics into an independent producer
    # node once shared ingestion caching is available.
    evaluations: list[_sweep.Evaluation] = [
        ReviewEvaluation(_common.reviewer_factory(ctx), goal)
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
    """Reviews each page with one model call per session."""

    name = "session_review"

    def __init__(
        self, reviewer: Callable[[str], str], goal: InvestigationGoal
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
        # Fail the sweep if every session review errored, indicating an infrastructure outage.
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
        """First encountered review exception, re-raised if all reviews fail."""


def _review_page(
    state: ADKStateLike,
    page: Page,
    revision_cache: AgentRevisionCache,
    reviewer: Callable[[str], str],
    tally: ReviewStats,
    *,
    outcomes: dict[str, str],
    goal: str,
) -> None:
    """Review sessions in a page and append resulting FindingSet to state.

    The page carries the deployment revision each session ran on, which keys the
    agent-configuration cache and attributes findings to release builds.

    Args:
        state: Workflow execution state.
        page: Ingested telemetry page.
        revision_cache: Cache for agent instructions and tool configurations.
        reviewer: Reviewer model callable.
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
        results = list(
            pool.map(
                lambda case: _review_case(
                    case,
                    revisions,
                    revision_cache,
                    reviewer,
                    session_review_focus=focus,
                    goal=goal,
                ),
                eval_cases,
            )
        )

    findings: list[Finding] = []
    sessions: dict[str, list[dict]] = {}
    agent_revisions: dict[str, list[AgentRevision]] = {}
    keys = build_outcome_keys(eval_cases, tally.reviewed)
    # Preserve deterministic page order when aggregating findings across threads.
    for position, (eval_case, (found, case_revisions, error)) in enumerate(
        zip(eval_cases, results, strict=True)
    ):
        tally.reviewed += 1
        case_id = str(getattr(eval_case, "eval_case_id", "") or "")
        outcome_key = keys[position]
        if error is not None:
            tally.errored += 1
            outcomes[outcome_key] = "errored"
            if tally.first_error is None:
                tally.first_error = error
            continue
        if not found:
            tally.passed += 1
            outcomes[outcome_key] = "passed"
            continue
        tally.failed += 1
        outcomes[outcome_key] = "failed"
        session_id = case_id
        revision = revisions.get(session_id, "")
        findings.extend(
            finding.model_copy(update={"agent_revision": revision})
            if revision
            else finding
            for finding in found
        )
        if session_id and session_id not in sessions:
            sessions[session_id] = session_review.extract_session_turns(
                eval_case
            )
            if case_revisions:
                agent_revisions[session_id] = case_revisions

    tally.findings += len(findings)
    tally.pages += 1
    # Reassign finding_sets list to trigger ADK state delta change tracking.
    state["finding_sets"] = [
        *(state.get("finding_sets") or []),
        FindingSet(
            findings=findings,
            sessions=sessions,
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
    reviewer: Callable[[str], str],
    *,
    session_review_focus: str,
    goal: str,
) -> tuple[list[Finding], list[AgentRevision], Exception | None]:
    """Review one session, returning findings, configurations, and any error.

    Errors are returned rather than raised to isolate individual failures.

    Args:
        eval_case: Individual evaluation case representing a conversation.
        revisions: Mapping of session IDs to agent revision strings.
        revision_cache: Cache for parsed agent configurations.
        reviewer: Reviewer model callable.
        session_review_focus: Review focus prompt instructions.
        goal: The developer's goal to review with; empty for none.

    Returns:
        Tuple of (extracted findings, agent revisions, caught exception or None).
    """
    session_id = str(getattr(eval_case, "eval_case_id", "") or "")
    try:
        case_revisions = revision_cache.build_revisions(
            eval_case, revisions.get(session_id, "")
        )
        return (
            session_review.extract_findings(
                eval_case,
                case_revisions,
                reviewer,
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
        )
        return [], [], exc
