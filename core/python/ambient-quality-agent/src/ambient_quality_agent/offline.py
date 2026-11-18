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

"""Runs AQuA's production investigation offline, over cases the caller supplies.

Wiring::

    run_investigation() ──► run_investigation_async()
                                │ _RUN_LOCK, backends.override_bindings()
                                ▼
    telemetry_fetcher ──► the caller's fetcher factory
    goal_loader ──► the caller's goal
    session_reviewer ──► the caller's reviewer, or the provider's (agentic by default)
    cluster_call, merge_call,
    verification_call, match_call ──► ModelCalls
    insight_writer ──► the caller's writer, or InMemoryInsightStore
    trajectory_recorder ──► NullTrajectoryRecorder
    event_writer, metrics_store ──► nothing
                                │ Runner.run_async(), InMemorySessionService
                                ▼
            investigation_workflow | _build_producer_workflow() ──► OfflineRun

For evaluation jobs and tuning harnesses that measure the investigation on
recorded sessions. The graph is the deployed one, node for node (or, with
``skip_insights``, truncated before the insight stages). The composition root
(`backends`) binds session ingestion, persistence, session review, and model
calls for the duration of one run and restores them afterward. Nothing is read from or written to BigQuery, Cloud Storage or a
session database. Model calls go wherever `ModelCalls` sends them, by default
to the production calls; ``eval_service`` mode also calls the Gen AI evaluation
service, as in a deployment.

The bindings are process-wide, so runs in one process take turns: a run that
starts while another is under way, in another task or thread, waits for it to
finish.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import functools
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from ambient_quality_agent import backends
from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import config
from ambient_quality_agent.core.nodes import _common, insight_correlation_node
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.core.workflow import investigation_workflow
from ambient_quality_agent.standalone.insights import InMemoryInsightStore
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.tools.documents.goal import InvestigationGoal
from ambient_quality_agent.tools.insights import matching
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
)
from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import Workflow
from google.genai import types as genai_types

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ambient_quality_agent.tools.ingestion.base import BaseFetcher
    from ambient_quality_agent.tools.insights.store import InsightWriter

ModelCall = Callable[[str], str]
"""One stage's model call: a prompt in, the model's raw JSON text out."""

AnalysisMode = Literal["session_review", "eval_service"]
"""How a run scores sessions (`WorkflowState.quality_analysis_mode`)."""

_APP_NAME = "aqa-offline"
_USER_ID = "aqa-offline"

_RUN_LOCK = threading.Lock()
"""Held for the length of one run, by whichever task or thread is running it."""

_RUN_LOCK_POLL_SECONDS = 0.05


@dataclasses.dataclass(frozen=True)
class ModelCalls:
    """The model call each stage of an offline run makes.

    A `None` field keeps that stage's current binding (the production call by
    default). Stages invoke these calls from thread pools, so each call must be
    thread-safe.
    """

    cluster: ModelCall | None = None
    """Groups findings into candidate clusters (`clustering.call_clustering_model`)."""

    merge: ModelCall | None = None
    """Merges candidate clusters that name one issue (`merge.call_merge_model`)."""

    verify: ModelCall | None = None
    """Verifies each candidate cluster (`verification.call_verification_model`)."""

    match: ModelCall | None = None
    """Matches candidates to existing insights (`matching.call_match_model`).
    Reached only through the default insight writer, or a given writer that
    asks `_common.match_call_factory` for its matcher."""


@dataclasses.dataclass(frozen=True)
class OfflineRun:
    """What one offline run leaves behind."""

    state: dict[str, Any]
    """The investigation's session state when the graph finished, as plain
    data: `WorkflowState` fields plus what the nodes wrote, such as
    ``counters``, ``finding_sets`` and ``progress_log``."""

    store: InMemoryStore
    """The store the default insight writer wrote to; empty when the run was
    given an insight writer of its own, or ran with ``skip_insights``."""


def run_investigation(**kwargs: Any) -> OfflineRun:
    """Runs `run_investigation_async` with ``kwargs`` to completion on a new event loop.

    Args:
        **kwargs: The keyword arguments of `run_investigation_async`, forwarded
            unchanged.

    Returns:
        The `OfflineRun` of the finished investigation.
    """
    return asyncio.run(run_investigation_async(**kwargs))


async def run_investigation_async(
    *,
    agent_name: str,
    investigation_id: str,
    fetcher: Callable[[], BaseFetcher],
    analysis_mode: AnalysisMode,
    budget: int,
    window: tuple[dt.datetime, dt.datetime] | None = None,
    model_calls: ModelCalls | None = None,
    goal: InvestigationGoal | None = None,
    insight_writer: InsightWriter | None = None,
    state_overrides: Mapping[str, Any] | None = None,
    session_reviewer: _common.SessionReviewFn | None = None,
    skip_insights: bool = False,
) -> OfflineRun:
    """Runs AQuA's investigation graph once over the given cases, offline.

    The providers the graph reaches are bound for the length of the run and
    given back on return, also when the run raises. They are process-wide, so
    a run waits until no other run in the process, in any task or thread, is
    under way; a caller's own `backends.override_bindings` block left open
    makes the run raise instead. Progress events stay in the returned state,
    no trajectory is recorded, and no published metric library is read, so
    only the session review or the configured eval metrics score sessions.
    What the fetcher, a model call or the insight writer raises stops the run
    and propagates unchanged.

    Args:
        agent_name: The observed agent's name, which the fetcher is asked for
            and the insights are stored under.
        investigation_id: The run's id, stamped on its insights and
            occurrences.
        fetcher: Builds a new fetcher of the sessions to investigate. Called
            with no arguments each time a sweep needs a fetcher: once in
            ``session_review`` mode, once per metric scope in ``eval_service``
            mode.
        analysis_mode: ``session_review`` reviews each session with the
            session reviewer (see `session_reviewer`); ``eval_service`` scores
            them with the eval metrics.
        budget: Most sessions to sample per metric scope.
        window: The start and end of the window the fetcher is asked for; the
            24 hours before the call by default.
        model_calls: Each stage's model call; `None` keeps every stage's
            current binding.
        goal: The developer's goal every review is sent with; `None` reviews
            without one and never reads a saved goal.
        insight_writer: Where the run's insights go. `None` writes them to an
            `InMemoryInsightStore` over `OfflineRun.store`, matching recurrences
            with the ``match`` call.
        state_overrides: Further `WorkflowState` fields to start the run with,
            such as ``multi_turn_metrics`` or ``insights_verification_enabled``.
            The fields the runner sets itself (the agent, the investigation
            id, the mode, the budget, the window, and the project and location
            from `config`) may not be among them.
        session_reviewer: Callable that reviews each session in
            ``session_review`` mode. Invoked concurrently across sessions from
            the review node's thread pool, so it must be thread-safe. `None`
            keeps the bound reviewer (the agentic reviewer in a deployment).
        skip_insights: Truncates the graph before `insight_correlation_node` so
            the run reviews or scores sessions without clustering, matching, or
            writing insights.

    Returns:
        The final state and the in-memory store.

    Raises:
        ValueError: If `state_overrides` names an unknown `WorkflowState` field
            or one set by the runner, or if `skip_insights` is set and
            `investigation_workflow` has no edge to `insight_correlation_node`
            to cut. Validation happens before binding providers.
        pydantic.ValidationError: If a field's value does not fit
            `WorkflowState`; nothing is bound then.
        RuntimeError: If a `backends.override_bindings` block is open, or the
            run's session is gone when the graph finishes.
    """
    seed = _build_seed_state(
        agent_name=agent_name,
        investigation_id=investigation_id,
        analysis_mode=analysis_mode,
        budget=budget,
        window=window,
        state_overrides=state_overrides or {},
    )
    store = InMemoryStore()
    bindings = _build_bindings(
        fetcher,
        model_calls or ModelCalls(),
        session_reviewer,
        goal or InvestigationGoal(),
        insight_writer,
        store,
    )
    node = (
        _build_producer_workflow(investigation_workflow)
        if skip_insights
        else investigation_workflow
    )
    # Polled rather than acquired on a worker thread, so a run cancelled while
    # it waits never leaves the lock held, and a blocked wait never stalls the
    # event loop the running run needs.
    while not _RUN_LOCK.acquire(blocking=False):
        await asyncio.sleep(_RUN_LOCK_POLL_SECONDS)
    try:
        with backends.override_bindings(bindings):
            state = await _drive_graph(node, investigation_id, seed)
    finally:
        _RUN_LOCK.release()
    return OfflineRun(state=state, store=store)


def _build_seed_state(
    *,
    agent_name: str,
    investigation_id: str,
    analysis_mode: AnalysisMode,
    budget: int,
    window: tuple[dt.datetime, dt.datetime] | None,
    state_overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Builds the session state the run starts from.

    Args:
        agent_name: The observed agent's name.
        investigation_id: The run's id.
        analysis_mode: `WorkflowState.quality_analysis_mode`.
        budget: `WorkflowState.budget_per_metric`.
        window: Window start and end, or `None` for the last 24 hours.
        state_overrides: Further `WorkflowState` fields, none of them one
            this sets.

    Returns:
        The state as JSON-ready data.

    Raises:
        ValueError: If `state_overrides` names a field `WorkflowState` lacks or
            one this sets.
    """
    unknown = set(state_overrides) - set(WorkflowState.model_fields)
    if unknown:
        raise ValueError(f"unknown WorkflowState field(s): {sorted(unknown)}")
    if window is None:
        now = dt.datetime.now(dt.UTC)
        window = (now - dt.timedelta(days=1), now)
    fields: dict[str, Any] = {
        "observed_agent_name": agent_name,
        "project_id": config.project_id,
        "location": config.location,
        "quality_analysis_mode": analysis_mode,
        "investigation_id": investigation_id,
        "budget_per_metric": budget,
        "window_start": window[0],
        "window_end": window[1],
    }
    taken = set(state_overrides) & set(fields)
    if taken:
        raise ValueError(
            f"state_overrides may not change field(s) set by the runner: {sorted(taken)}"
        )
    return WorkflowState(**fields, **state_overrides).model_dump(mode="json")


def _build_bindings(
    fetcher: Callable[[], BaseFetcher],
    model_calls: ModelCalls,
    session_reviewer: _common.SessionReviewFn | None,
    goal: InvestigationGoal,
    insight_writer: InsightWriter | None,
    store: InMemoryStore,
) -> dict[str, Callable[..., Any]]:
    """Builds the provider bindings of one offline run.

    Args:
        fetcher: A no-argument factory of fetchers.
        model_calls: The stages' model calls.
        session_reviewer: The caller's session reviewer, or `None` to keep
            the bound one.
        goal: The goal every review is sent with.
        insight_writer: The caller's insight writer, or `None` for one over
            `store`.
        store: The store the default insight writer writes to.

    Returns:
        Bindings for `backends.override_bindings`.
    """

    def build_insight_writer(ctx: Any) -> InsightWriter:
        if insight_writer is not None:
            return insight_writer
        return InMemoryInsightStore(
            store,
            observed_agent_key=config_module.extract_agent_key(ctx.state),
            matcher=functools.partial(
                matching.match_all_candidates,
                model_call=_common.match_call_factory(ctx),
            ),
        )

    stage_calls = {
        "cluster_call": model_calls.cluster,
        "merge_call": model_calls.merge,
        "verification_call": model_calls.verify,
        "match_call": model_calls.match,
    }
    bindings: dict[str, Callable[..., Any]] = {
        "telemetry_fetcher": lambda ctx: fetcher(),
        "goal_loader": lambda ctx: goal,
        **{
            name: lambda ctx, call=call: call
            for name, call in stage_calls.items()
            if call is not None
        },
        "insight_writer": build_insight_writer,
        "trajectory_recorder": lambda ctx: NullTrajectoryRecorder(),
        "event_writer": lambda state, text, source: None,
        "metrics_store": lambda: None,
    }
    if session_reviewer is not None:
        bindings["session_reviewer"] = lambda ctx: session_reviewer
    return bindings


def _build_producer_workflow(workflow: Workflow) -> Workflow:
    """Truncates a workflow before `insight_correlation_node`.

    Extracts edges from the production graph so producer nodes and routing
    match deployed behavior:
    1. Truncates each edge chain before `insight_correlation_node`.
    2. Drops chains with fewer than two nodes.
    3. Copies all other set workflow fields.

    Args:
        workflow: The production investigation workflow.

    Returns:
        Workflow containing the producer nodes and routes, terminating after
        the producer stages.

    Raises:
        ValueError: If no edge chain leads to `insight_correlation_node`, or if
            `insight_correlation_node` remains reachable in the truncated graph.
    """
    edges: list[Any] = []
    found = False
    for edge in workflow.edges:
        chain = list(edge)
        # Match by object identity because node models compare equal by field values.
        cut = next(
            (
                i
                for i, item in enumerate(chain)
                if item is insight_correlation_node
            ),
            None,
        )
        if cut is not None:
            found = True
            chain = chain[:cut]
        if len(chain) >= 2:
            edges.append(tuple(chain))
    copied = {
        field: getattr(workflow, field)
        for field in workflow.model_fields_set
        - {"edges", "graph", "name", "description"}
    }
    producer = Workflow(
        name=f"{workflow.name}_producers",
        description="The investigation workflow up to its insight stages.",
        edges=edges,
        **copied,
    )
    if not found or any(
        node is insight_correlation_node
        for node in (producer.graph.nodes if producer.graph else ())
    ):
        raise ValueError(
            f"{workflow.name} has no edge to insight_correlation that this cut "
            "removes; update src/ambient_quality_agent/offline.py to match "
            "core/workflow.py."
        )
    return producer


async def _drive_graph(
    node: Workflow, investigation_id: str, seed: dict[str, Any]
) -> dict[str, Any]:
    """Runs an investigation graph once in an in-memory session.

    Args:
        node: The graph to run: `investigation_workflow`, or its cut before
            the insight stages.
        investigation_id: The run's id, used as the session id.
        seed: The session state to start from.

    Returns:
        The session's state after the run.
    """
    runner = Runner(
        app_name=_APP_NAME,
        node=node,
        session_service=InMemorySessionService(),
    )
    await runner.session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=investigation_id,
        state=seed,
    )
    async for _ in runner.run_async(
        user_id=_USER_ID,
        session_id=investigation_id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text="run")]
        ),
    ):
        pass
    session = await runner.session_service.get_session(
        app_name=_APP_NAME, user_id=_USER_ID, session_id=investigation_id
    )
    if session is None:
        raise RuntimeError(
            f"offline session {investigation_id!r} is gone after the run"
        )
    return dict(session.state)
