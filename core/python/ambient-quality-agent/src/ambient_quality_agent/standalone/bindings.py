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

"""Storage provider bindings for standalone execution mode.

Maps storage providers to in-memory adapters backed by a shared `InMemoryStore`,
and the jobs store to files under `.aqua/job/`.
`backends.configure_providers` registers these factories.

## Shared Store Instance

Investigation sweeps run on background threads while API handlers serve requests
concurrently. All provider bindings close over a single `InMemoryStore` instance
so mutations written by a sweep are immediately visible to API routes.

`InMemoryStore` supports this through a single write lock per mutation and lock-free
reads.

## Unaffected Subsystems

- **Telemetry**: Fetches telemetry directly from the deployed target agent
  under Application Default Credentials.
- **Model calls**: Clustering, merging, verification, and review continue to call
  Gemini platform. Standalone mode eliminates persisted cloud storage rather than API calls.
- **Goal, memories and attached agent configuration**: Files under
  `files.JOBS_DIR` in place of the jobs bucket, laid out as the bucket is. They
  outlive the process, unlike the in-memory store.
- **Source snapshot and metric library**: Degrade gracefully when cloud storage
  is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.standalone.files import (
    JOBS_DIR,
    SOURCE_DIR_NAME,
    FileObjectStore,
)
from ambient_quality_agent.standalone.insight_reader import (
    InMemoryInsightReader,
)
from ambient_quality_agent.standalone.insights import InMemoryInsightStore
from ambient_quality_agent.standalone.investigations import (
    InMemoryInvestigationStore,
)
from ambient_quality_agent.standalone.root_causes import InMemoryRootCauseStore
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.standalone.trajectories import (
    InMemoryTrajectoryReader,
    InMemoryTrajectoryStore,
)
from ambient_quality_agent.tools.insights import matching
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.trajectories.recorder import TrajectoryRecorder

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def create_store() -> InMemoryStore:
    """Creates an empty in-memory store for a standalone session.

    Returns:
        A new `InMemoryStore` instance with no prior history.
    """
    return InMemoryStore()


def resolve_jobs_dir() -> Path:
    """Returns the directory a standalone run keeps its files in.

    Returns:
        `files.JOBS_DIR` under the current working directory.
    """
    return Path.cwd() / JOBS_DIR


def build_bindings(
    store: InMemoryStore, *, jobs_dir: Path
) -> dict[str, Callable[..., Any]]:
    """Builds storage provider factories keyed by provider name.

    Args:
        store: Shared in-memory store backing the dataset's adapters.
        jobs_dir: Directory holding the goal, the memories and the attached
            agents' configurations.

    Returns:
        Mapping of provider names matching `backends.PROVIDERS` to provider factory
        callables.
    """

    def resolve_observed_agent_name(state: Any) -> str:
        """Resolves the observed agent name from the effective configuration.

        Args:
            state: Request or execution state containing configuration.

        Returns:
            The configured observed agent name.
        """
        return effective_config.load(state).observed_agent_name

    jobs = FileObjectStore(jobs_dir)
    source = FileObjectStore(jobs_dir.parent / SOURCE_DIR_NAME)
    return {
        # Investigation lifecycle tracking and run metadata.
        "investigation_store": lambda state: InMemoryInvestigationStore(store),
        # Appends workflow progress events to the investigation store.
        "event_writer": lambda state, text, source: InMemoryInvestigationStore(
            store
        ).append_event(state["run_id"], text, source=source),
        # Writes sweep insights to memory while preserving model-based matching.
        "insight_writer": lambda ctx: InMemoryInsightStore(
            store,
            agent_name=ctx.state["observed_agent_name"],
            matcher=_build_matcher(ctx),
        ),
        "trajectory_recorder": lambda ctx: TrajectoryRecorder(
            store=InMemoryTrajectoryStore(
                store, agent_name=ctx.state["observed_agent_name"]
            ),
            run_id=ctx.state.get("run_id", ""),
            agent_name=ctx.state["observed_agent_name"],
            source=ctx.state.get("telemetry_ingestion_source", ""),
        ),
        "root_cause_writer": lambda state: InMemoryRootCauseStore(store),
        # Read-only and interactive interfaces for dashboard and chat tools.
        "insight_reader": lambda state: InMemoryInsightReader(
            store, agent_name=resolve_observed_agent_name(state)
        ),
        "insight_store": lambda state: InMemoryInsightStore(
            store, agent_name=resolve_observed_agent_name(state)
        ),
        "trajectory_reader": lambda state: InMemoryTrajectoryReader(
            store, agent_name=resolve_observed_agent_name(state)
        ),
        # A preview's conversations join the sweeps' in the one archive, as they
        # do in BigQuery's `trajectory_payloads`.
        "preview_trajectory_store": lambda config: InMemoryTrajectoryStore(
            store, agent_name=config.observed_agent_name
        ),
        # The goal, the memories and the attached agents' configurations.
        "jobs_store": lambda: jobs,
        # The observed agents' source snapshots, beside the jobs directory.
        "source_store": lambda: source,
    }


def _build_matcher(ctx: Any) -> matching.Matcher:
    """Builds a candidate matcher using the model call configured in the context.

    Imports `_common` locally to prevent circular dependencies between the
    adapters and provider setup.

    Args:
        ctx: Workflow execution context containing model provider bindings.

    Returns:
        A callable matching candidate issue clusters against existing insights.
    """
    from ambient_quality_agent.core.nodes import _common

    model_call = _common.match_call_factory(ctx)

    def match_candidates(
        candidates: Sequence[str], existing: Sequence[str]
    ) -> dict[int, int | None]:
        return matching.match_candidates(
            candidates, existing, model_call=model_call
        )

    return match_candidates
