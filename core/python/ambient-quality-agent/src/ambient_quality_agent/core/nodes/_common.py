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

"""Shared logic for workflow nodes."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from ambient_quality_agent import providers
from ambient_quality_agent.core._memory import MemoryGuard
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.core.state import add_counts
from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.evaluation.agent_platform_eval import (
    AgentPlatformEvalService,
)
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import (
    BaseFetcher,
    CachingGcsReader,
    GcsClientReader,
)
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from ambient_quality_agent.tools.insights import (
    clustering,
    matching,
    merge,
    verification,
)
from ambient_quality_agent.tools.insights.store import InsightWriter
from ambient_quality_agent.tools.metrics.gcs_library import (
    load_library_from_gcs,
)
from ambient_quality_agent.tools.metrics.library import MetricLibrary
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.review import session_review
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
    TrajectoryRecorder,
)
from google.adk.agents.context import Context
from google.cloud import storage

logger = logging.getLogger(__name__)


def _build_gcs_reader() -> CachingGcsReader:
    """Assemble the GCS ref reader: storage client wrapped with caching.

    Returns:
        CachingGcsReader instance.
    """
    return CachingGcsReader(GcsClientReader(storage.Client()))


def _build_memory_guard(ctx: LaunchContext) -> Callable[[], bool]:
    """Build the memory guard for this scope's fetcher.

    Seeded with the run's start-of-investigation memory baseline (`state`
    key ``memory_baseline_mb``) so multi-turn and single-turn scopes measure
    growth from the same baseline.

    Args:
        ctx: Launch context containing workflow state.

    Returns:
        Callable returning True when memory limits are reached.
    """
    baseline_mb = ctx.state.get("memory_baseline_mb")
    return MemoryGuard(baseline_mb=baseline_mb).should_stop


def _create_default_fetcher(ctx: Context) -> BaseFetcher:
    """Build the default telemetry fetcher for a sweep run.

    Args:
        ctx: Workflow context supplying configuration state.

    Returns:
        The configured BaseFetcher instance.
    """
    return build_fetcher(ctx)


def build_fetcher(
    ctx: LaunchContext,
    *,
    recorder: TrajectoryRecorder | NullTrajectoryRecorder | None = None,
    job_timeout_ms: int | None = None,
) -> BaseFetcher:
    """Build the telemetry fetcher configured in `telemetry_ingestion_source`.

    Supports ``big_query`` (ADK analytics), ``cloud_ops`` (Cloud Trace linked
    dataset), and ``cloud_logging`` (Cloud Logging GenAI export table).

    Args:
        ctx: Workflow context supplying configuration state.
        recorder: Optional custom recorder for sampled trajectories. When
            omitted, instantiates a default recorder from context.
        job_timeout_ms: Server-side execution timeout in milliseconds for
            BigQuery query jobs.

    Returns:
        The configured BaseFetcher instance.

    Raises:
        ValueError: If the ingestion source is unsupported or the telemetry
            dataset is not configured.
    """
    state = ctx.state
    source = state.get("telemetry_ingestion_source", "big_query")
    if source not in ("big_query", "cloud_ops", "cloud_logging"):
        raise ValueError(
            f"Unsupported telemetry_ingestion_source: {source!r}. "
            "Expected 'big_query', 'cloud_ops', or 'cloud_logging'."
        )
    dataset = state.get("telemetry_dataset", "")
    if not dataset:
        raise ValueError(
            f"telemetry_ingestion_source {source!r} requires "
            "telemetry_dataset (AQA_TELEMETRY_DATASET) to be set."
        )
    table = state.get("telemetry_table", "")
    location = state.get("telemetry_location", "us-central1")
    # Empty or absent means the telemetry is in AQuA's own project.
    observed_project_id = state.get("observed_project_id") or ""
    # Fetchers default to random sampling across the time window when selector_sql is empty.
    selector_sql = state.get("selector_sql", "")
    selector_ai_model = state.get("selector_ai_model", "")
    reporting_to = (
        trajectory_recorder_factory(ctx) if recorder is None else recorder
    )
    if source == "big_query":
        return BigQueryFetcher(
            project_id=state["project_id"],
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=observed_project_id,
            memory_guard=_build_memory_guard(ctx),
            recorder=reporting_to,
            selector_sql=selector_sql,
            selector_ai_model=selector_ai_model,
            job_timeout_ms=job_timeout_ms,
        )
    if source == "cloud_ops":
        return CloudOpsFetcher(
            project_id=state["project_id"],
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=observed_project_id,
            gcs_reader=_build_gcs_reader(),
            memory_guard=_build_memory_guard(ctx),
            recorder=reporting_to,
            selector_sql=selector_sql,
            selector_ai_model=selector_ai_model,
            job_timeout_ms=job_timeout_ms,
        )
    return CloudLoggingFetcher(
        project_id=state["project_id"],
        dataset=dataset,
        table=table,
        location=location,
        observed_project_id=observed_project_id,
        gcs_reader=_build_gcs_reader(),
        memory_guard=_build_memory_guard(ctx),
        recorder=reporting_to,
        selector_sql=selector_sql,
        selector_ai_model=selector_ai_model,
        job_timeout_ms=job_timeout_ms,
    )


def _create_default_evaluator(ctx: Context) -> AgentPlatformEvalService:
    """Build an `AgentPlatformEvalService` from the workflow context.

    Args:
        ctx: Workflow context supplying project and location.

    Returns:
        Configured AgentPlatformEvalService instance.
    """
    state = ctx.state
    return AgentPlatformEvalService(
        project_id=state["project_id"],
        location=state.get("location", "us-central1"),
    )


def _create_default_reviewer(ctx: Context) -> Callable[[str], str]:
    """Provide the default session-review LLM callable (`prompt -> raw JSON`).

    Serves as the review producer counterpart to `evaluator_factory`, enabling
    test fakes to intercept model invocations during graph execution.

    Args:
        ctx: Workflow context.

    Returns:
        Callable taking prompt string and returning raw model response JSON string.
    """
    return session_review.call_review_model


def _load_default_goal(ctx: Context) -> goal_doc.InvestigationGoal:
    """Loads the developer's goal from the jobs store.

    Args:
        ctx: Workflow context.

    Returns:
        The goal the session review uses. Never raises: a goal that cannot be
        read gives no goal.
    """
    del ctx  # The jobs store is the deployment's, not the run's.
    return goal_doc.load_investigation_goal(objects.jobs_store_factory())


def _create_default_model_call(ctx: Context) -> Callable[[str], str]:
    """Build the clustering LLM call callable (`prompt -> raw JSON`).

    Args:
        ctx: Workflow context.

    Returns:
        Callable taking prompt string and returning raw model response JSON string.
    """
    return clustering.call_clustering_model


def _create_default_merge_call(ctx: Context) -> Callable[[str], str]:
    """Build the candidate-merging LLM call callable.

    Separate from the clustering call because it answers a different schema; a
    seam of its own so a test can fake it without reaching the network.

    Args:
        ctx: Workflow context.

    Returns:
        Callable taking prompt string and returning raw model response JSON string.
    """
    return merge.call_merge_model


def _create_default_match_call(ctx: Context) -> Callable[[str], str]:
    """Build the recurrence-matching LLM call callable.

    Its own seam, like the merging and verification calls. It answers a schema
    of its own on a model of its own, so a test fakes it without a network.

    Args:
        ctx: Workflow context.

    Returns:
        Callable taking prompt string and returning raw model response JSON string.
    """
    return matching.call_match_model


def _create_default_verification_call(ctx: Context) -> Callable[[str], str]:
    """Build the cluster-verification LLM call callable.

    Its own seam, like the merging call: the pass answers a third schema and
    runs on a model of its own, so a test fakes it without reaching the network.

    Args:
        ctx: Workflow context.

    Returns:
        Callable taking prompt string and returning raw model response JSON string.
    """
    return verification.call_verification_model


def add_ingestion_counts(state: ADKStateLike, fetcher: BaseFetcher) -> None:
    """Add fetcher ingestion tallies into workflow run counters.

    Args:
        state: Workflow state dictionary.
        fetcher: Telemetry fetcher providing ingestion counters.
    """
    counts = fetcher.counts
    add_counts(
        state,
        traces_scanned=counts.scanned,
        traces_ingested=counts.ingested,
        traces_ingested_partial=counts.partial,
        traces_ingested_failed=counts.failed,
    )


def refuse_unvalidated_selector(
    fetcher: BaseFetcher,
    *,
    node_name: str,
    agent_name: str,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
) -> None:
    """Validate selector SQL before query execution.

    Durable runs reconstruct state from database records across execution
    boundaries without prior validation proofs. Performing a dry run ensures
    agent-authored SQL cannot execute unauthorized queries under the engine
    service account's credentials. Runs without a custom selector skip this check.

    Args:
        fetcher: Telemetry fetcher configured for the current sweep.
        node_name: Workflow node name for error reporting.
        agent_name: Watched agent name bound as `@agent_name`.
        start: Window start timestamp bound as `@window_start`.
        end: Window end timestamp bound as `@window_end`.
        limit: Sampling budget limit bound as `@limit`.

    Raises:
        selector.SelectorRefused: If the selector fails lexical or dry-run
            validation.
    """
    if not fetcher.has_selector:
        return
    rejection = fetcher.precheck_selector(
        agent_name, start, end, limit=limit
    ).rejection
    if rejection is None:
        return
    logger.error(
        "%s: refusing this run's selector (%s).",
        node_name,
        rejection.reason.value,
    )
    raise selector.SelectorRefused(rejection)


def count_scanned(
    fetcher: BaseFetcher,
    *,
    node_name: str,
    agent_name: str,
    metric_type: MetricType,
    start: dt.datetime,
    end: dt.datetime,
) -> int | None:
    """Count candidate traces in the window before budget sampling.

    Args:
        fetcher: Telemetry fetcher for the target source.
        node_name: Current node name for logging.
        agent_name: Target agent name.
        metric_type: Metric scope (single-turn or multi-turn).
        start: Window start timestamp.
        end: Window end timestamp.

    Returns:
        How many traces the window holds, or `None` when the count could not be
        taken. The two are different facts -- ``0`` says the window held
        nothing, `None` says nothing is known -- and a caller that reports an
        empty window must not report one it failed to measure.
    """
    try:
        return fetcher.count_scanned(agent_name, metric_type, start, end)
    except Exception as exc:
        logger.warning(
            "%s: could not count the traces in the window: %s", node_name, exc
        )
        return None


def count_by_agent(
    fetcher: BaseFetcher,
    *,
    node_name: str,
    metric_type: MetricType,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, int] | None:
    """Count the traces in the window per agent name, for an error message.

    Args:
        fetcher: Telemetry fetcher for the target source.
        node_name: Current node name for logging.
        metric_type: Metric scope (single-turn or multi-turn).
        start: Window start timestamp.
        end: Window end timestamp.

    Returns:
        Trace counts keyed by agent name, or `None` when they could not be
        taken. The caller is already reporting a failure, and this only adds
        detail to it, so a failed count must not replace that failure.
    """
    try:
        return fetcher.count_by_agent(metric_type, start, end)
    except Exception as exc:
        logger.warning(
            "%s: could not count the traces per agent: %s", node_name, exc
        )
        return None


fetcher_factory: Callable[[Context], BaseFetcher] = _create_default_fetcher
evaluator_factory: Callable[[Context], AgentPlatformEvalService] = (
    _create_default_evaluator
)
reviewer_factory: Callable[[Context], Callable[[str], str]] = (
    _create_default_reviewer
)
goal_loader: Callable[[Context], goal_doc.InvestigationGoal] = (
    _load_default_goal
)
model_call_factory: Callable[[Context], Callable[[str], str]] = (
    _create_default_model_call
)
merge_call_factory: Callable[[Context], Callable[[str], str]] = (
    _create_default_merge_call
)
match_call_factory: Callable[[Context], Callable[[str], str]] = (
    _create_default_match_call
)
verification_call_factory: Callable[[Context], Callable[[str], str]] = (
    _create_default_verification_call
)
insight_store_factory: Callable[[Context], InsightWriter] = (
    providers.build_unbound_provider("insight_store_factory")
)
trajectory_recorder_factory: Callable[[LaunchContext], TrajectoryRecorder] = (
    providers.build_unbound_provider("trajectory_recorder_factory")
)


def _create_default_metric_library(ctx: Context) -> tuple[MetricLibrary, str]:
    """Load the code-metric library from GCS, returning `(library, warning)`.

    Read errors return a markdown warning instead of raising. Because the
    window only advances on completion, raising would wedge future sweeps on
    user upload errors.

    Args:
        ctx: Workflow context providing GCS bucket configuration.

    Returns:
        Tuple of (loaded `MetricLibrary`, markdown warning string if errors occurred).
    """
    bucket = ctx.state.get("metrics_gcs_bucket", "")
    if not bucket:
        return MetricLibrary(), ""
    try:
        library = load_library_from_gcs(bucket)
        # Report load failures in warning text so surviving metrics proceed without appearing
        # as a complete library execution.
        if library.failures:
            named = ", ".join(
                f"`{name}` ({why})" for name, why in library.failures
            )
            return library, (
                f" **{len(library.failures)} metric(s) did not load** and scored "
                f"nothing: {named}."
            )
        return library, ""
    # Catch SystemExit to isolate scripts calling sys.exit or argparse at module import scope.
    except (Exception, SystemExit) as exc:
        logger.exception(
            "Could not read the metric library from gs://%s.", bucket
        )
        return MetricLibrary(), (
            f" **The metric library at `gs://{bucket}` could not be read** "
            f"({type(exc).__name__}); no session was scored."
        )


metric_library_factory: Callable[[Context], tuple[MetricLibrary, str]] = (
    _create_default_metric_library
)
