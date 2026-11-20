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

"""Where AQA's storage lives, chosen once at start-up.

Wiring::

    fast_api_app._lifespan(), local_run.main()        tests
        │ configure_providers()                         │ override_providers()
        └───────────────┬───────────────────────────────┘
        ┌── deployed ───┴── standalone ───────────┐
        ▼                                         ▼
    build_deployed_bindings()                 standalone.bindings
      build_bigquery_bindings()                 create_store() ──► InMemoryStore
        BigQueryInvestigationStore,             build_bindings()
        BigQueryInsightStore,                     InMemory* adapters,
        BigQueryRootCauseStore,                   FileObjectStore(.aqua/job/),
        TrajectoryRecorder,                       FileObjectStore(.aqua/source/),
                                                  FileObjectStore(.aqua/metrics/)
        BigQueryTrajectoryStore,
        BigQueryInsightReader,
        BigQueryTrajectoryReader
      build_gcs_bindings()
        GcsObjectStore(jobs bucket),
        GcsObjectStore(source bucket),
        GcsObjectStore(metrics bucket)
        │                                         │
        └───────────────► bind_providers() ◄──────┘
                              │ sets each PROVIDERS target
                              ▼
    investigation.model.store_factory, core.state.event_writer,
    _common.insight_store_factory, _common.trajectory_recorder_factory,
    root_cause_tools.store_factory, insight_tools.reader_factory,
    insight_tools.store_factory, trajectory_tools.reader_factory,
    investigation.preview.store_factory, objects.store.jobs_store_factory,
    objects.store.source_store_factory, objects.store.metrics_store_factory

    offline.run_investigation_async(), tests
        │ override_bindings()
        ▼
    any PROVIDERS target, including the pipeline providers no backend binds:
    _common.fetcher_factory, _common.goal_loader,
    _common.session_reviewer_factory, _common.model_call_factory,
    _common.merge_call_factory, _common.verification_call_factory,
    _common.match_call_factory

This is the composition root. Each store is reached through a provider: a
module attribute holding its factory, which callers look up when they need one
(a service locator). `configure_providers()` binds every provider at start-up,
from one set of bindings per backend.

Two backends bind the same providers:

- **Deployed**: BigQuery for AQuA's dataset, the jobs bucket for the goal,
  the memories and the attached agents' configurations, the source bucket
  for the observed agents' source snapshots, and the metrics bucket for the
  code-metric library.
- **Standalone**: one in-memory store in place of the dataset, and files under
  `.aqua/job/`, `.aqua/source/` and `.aqua/metrics/` in place of the buckets,
  so a standalone run keeps nothing in a Cloud project.

`configure_providers()` reads `Config.standalone` and binds one set. Every entry
point calls it, and nothing else needs to. Telemetry, the goal and the model
calls are providers too (`PIPELINE_PROVIDERS`), but no backend binds them, as
both backends reach them the same way; `override_bindings()` rebinds them for
an offline run. This is the only module that imports
both backends; the modules that own a provider declare it and nothing more.

## An unbound provider raises

`providers.build_unbound_provider` is what a provider holds until it is bound.
An entry point that binds nothing therefore fails at its first write, naming
the provider.

The alternative is worse. A provider that defaulted to BigQuery would work in a
deployment and be wrong in a standalone run, with nothing to say so: the investigation
reports success and the rows go to a real project's tables.

## `configure_providers()` is called, not imported

Two rules, and they are the same rule:

- It does not live in `config.py`. `core/state.py` imports `config`, so a
  `config` that imported the provider owners to bind them would close a cycle.
- It does not run at module import. Importing a module must not rewire global
  state, or a test that imports the app loses the fakes it installed.

So an entry point calls it at start-up, and a process that skips it gets the
error above rather than a guess.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

from ambient_quality_agent import clients
from ambient_quality_agent import config as config_module
from ambient_quality_agent.core import root_cause_tools
from ambient_quality_agent.core import state as core_state
from ambient_quality_agent.core.investigation import (
    model as investigation_model,
)
from ambient_quality_agent.core.investigation import (
    preview as investigation_preview,
)
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.standalone import bindings as standalone_bindings
from ambient_quality_agent.tools import gcs
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.bigquery_root_cause_store import (
    BigQueryRootCauseStore,
)
from ambient_quality_agent.tools.insights.bigquery_store import (
    BigQueryInsightStore,
)
from ambient_quality_agent.tools.investigations.bigquery_store import (
    BigQueryInvestigationStore,
)
from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.orchestrator import (
    insight_tools,
    trajectory_tools,
)
from ambient_quality_agent.tools.trajectories.bigquery_reader import (
    BigQueryTrajectoryReader,
)
from ambient_quality_agent.tools.trajectories.bigquery_store import (
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.recorder import TrajectoryRecorder
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from ambient_quality_agent.standalone.store import InMemoryStore

logger = logging.getLogger(__name__)


class ProviderTarget(NamedTuple):
    """The module attribute a provider lives in, as `mock.patch.object` names it."""

    module: Any
    attribute: str


_PIPELINE_TARGETS: dict[str, ProviderTarget] = {
    "telemetry_fetcher": ProviderTarget(_common, "fetcher_factory"),
    "goal_loader": ProviderTarget(_common, "goal_loader"),
    "session_reviewer": ProviderTarget(_common, "session_reviewer_factory"),
    "cluster_call": ProviderTarget(_common, "model_call_factory"),
    "merge_call": ProviderTarget(_common, "merge_call_factory"),
    "verification_call": ProviderTarget(_common, "verification_call_factory"),
    "match_call": ProviderTarget(_common, "match_call_factory"),
}

PROVIDERS: dict[str, ProviderTarget] = {
    "investigation_store": ProviderTarget(investigation_model, "store_factory"),
    "event_writer": ProviderTarget(core_state, "event_writer"),
    "insight_writer": ProviderTarget(_common, "insight_store_factory"),
    "trajectory_recorder": ProviderTarget(
        _common, "trajectory_recorder_factory"
    ),
    "root_cause_writer": ProviderTarget(root_cause_tools, "store_factory"),
    "insight_reader": ProviderTarget(insight_tools, "reader_factory"),
    "insight_store": ProviderTarget(insight_tools, "store_factory"),
    "trajectory_reader": ProviderTarget(trajectory_tools, "reader_factory"),
    "preview_trajectory_store": ProviderTarget(
        investigation_preview, "store_factory"
    ),
    "jobs_store": ProviderTarget(objects_store, "jobs_store_factory"),
    "source_store": ProviderTarget(objects_store, "source_store_factory"),
    "metrics_store": ProviderTarget(objects_store, "metrics_store_factory"),
    **_PIPELINE_TARGETS,
}
"""Every provider, keyed by what it provides rather than by where it lives.

One list, so the two backends cannot cover different sets. Each returns a
mapping over these keys less `PIPELINE_PROVIDERS`, and `bind_providers`
refuses one that does not.
"""

PIPELINE_PROVIDERS = frozenset(_PIPELINE_TARGETS)
"""The providers no backend binds: telemetry, the goal and the model calls.

A deployment and a standalone run reach these the same way, so each keeps the
production default it is declared with. `override_bindings` rebinds them, for
`offline.run_investigation` and for tests.
"""

_BINDINGS_HELD = threading.Lock()
"""Held while an `override_bindings` block is open, in any thread."""


def _create_bigquery_client(cfg: Any) -> bigquery.Client:
    """Creates a BigQuery client whose location fixes the job region.

    Args:
        cfg: Configuration containing project_id and aqa_dataset_location.

    Returns:
        Configured BigQuery client.
    """
    return clients.create_bigquery_client(
        cfg.project_id, cfg.aqa_dataset_location
    )


def build_bigquery_bindings() -> dict[str, Callable[..., Any]]:
    """Constructs provider bindings for the deployed BigQuery backend.

    Returns:
        Mapping of provider names to their factory callables.
    """

    def build_insight_writer(ctx: Any) -> Any:
        state = ctx.state
        location = state.get("aqa_dataset_location", "us-central1")
        return BigQueryInsightStore(
            client=clients.create_bigquery_client(
                state["project_id"], location
            ),
            project_id=state["project_id"],
            dataset=state.get("aqa_dataset", "aqua_insights"),
            observed_agent_key=config_module.extract_agent_key(state),
            match_call=_common.match_call_factory(ctx),
        )

    def build_trajectory_recorder(ctx: Any) -> TrajectoryRecorder:
        state = ctx.state
        location = state.get("aqa_dataset_location", "us-central1")
        return TrajectoryRecorder(
            store=BigQueryTrajectoryStore(
                client=clients.create_bigquery_client(
                    state["project_id"], location
                ),
                project_id=state["project_id"],
                dataset=state.get("aqa_dataset", "aqua_insights"),
                observed_agent_key=config_module.extract_agent_key(state),
            ),
            investigation_id=state.get("investigation_id", ""),
            observed_agent_key=config_module.extract_agent_key(state),
            source=state.get("telemetry_ingestion_source", ""),
        )

    def build_root_cause_writer(state: Any) -> BigQueryRootCauseStore:
        cfg = effective_config.load(state)
        return BigQueryRootCauseStore(
            client=_create_bigquery_client(cfg),
            project_id=cfg.project_id,
            dataset=cfg.aqa_dataset,
        )

    def build_insight_reader(state: Any) -> BigQueryInsightReader:
        cfg = effective_config.load(state)
        return BigQueryInsightReader(
            client=_create_bigquery_client(cfg),
            project_id=cfg.project_id,
            dataset=cfg.aqa_dataset,
            observed_agent_key=cfg.resolve_agent_key(),
        )

    def build_insight_store(state: Any) -> BigQueryInsightStore:
        # No match model: neither operator action calls `find_existing_insights`.
        cfg = effective_config.load(state)
        return BigQueryInsightStore(
            client=_create_bigquery_client(cfg),
            project_id=cfg.project_id,
            dataset=cfg.aqa_dataset,
            observed_agent_key=cfg.resolve_agent_key(),
        )

    def build_trajectory_reader(state: Any) -> BigQueryTrajectoryReader:
        cfg = effective_config.load(state)
        return BigQueryTrajectoryReader(
            client=_create_bigquery_client(cfg),
            project_id=cfg.project_id,
            dataset=cfg.aqa_dataset,
            observed_agent_key=cfg.resolve_agent_key(),
        )

    return {
        "investigation_store": lambda state: (
            BigQueryInvestigationStore.from_config(effective_config.load(state))
        ),
        # The nodes run on a flat snapshot of the config rather than the
        # session, so this one cannot go through `effective_config`.
        "event_writer": lambda state, text, source: (
            investigation_model.append_investigation_event(
                BigQueryInvestigationStore.from_workflow_state(state),
                state["investigation_id"],
                text,
                source=source,
            )
        ),
        "insight_writer": build_insight_writer,
        "trajectory_recorder": build_trajectory_recorder,
        "root_cause_writer": build_root_cause_writer,
        "insight_reader": build_insight_reader,
        "insight_store": build_insight_store,
        "trajectory_reader": build_trajectory_reader,
        # A custom-investigation preview stores the trajectory bundles it tried.
        "preview_trajectory_store": lambda config: BigQueryTrajectoryStore(
            client=_create_bigquery_client(config),
            project_id=config.project_id,
            dataset=config.aqa_dataset,
            observed_agent_key=config.resolve_agent_key(),
        ),
    }


def build_gcs_bindings() -> dict[str, Callable[..., Any]]:
    """Constructs provider bindings for the deployed Cloud Storage backend.

    The jobs, source and metrics buckets belong to the deployment rather than to
    an observed agent, so they are read once from the environment rather than
    per request. A deployment that names no source or metrics bucket has no
    such store.

    Returns:
        Mapping of provider names to their factory callables.
    """
    cfg = config_module.load()
    jobs = GcsObjectStore(cfg.jobs_gcs_bucket)
    source = (
        GcsObjectStore(
            cfg.source_gcs_bucket,
            client_factory=gcs.build_per_thread_client_factory(),
        )
        if cfg.source_gcs_bucket
        else None
    )
    metrics = (
        GcsObjectStore(cfg.metrics_gcs_bucket)
        if cfg.metrics_gcs_bucket
        else None
    )
    return {
        "jobs_store": lambda: jobs,
        "source_store": lambda: source,
        "metrics_store": lambda: metrics,
    }


def build_deployed_bindings() -> dict[str, Callable[..., Any]]:
    """Constructs every provider binding for a deployment: BigQuery and Cloud Storage.

    Returns:
        Mapping of provider names to their factory callables.
    """
    return {**build_bigquery_bindings(), **build_gcs_bindings()}


def bind_providers(bindings: dict[str, Callable[..., Any]]) -> dict[str, Any]:
    """Binds a backend's providers and returns what each held previously.

    A mapping that does not match `PROVIDERS` less `PIPELINE_PROVIDERS` exactly
    is refused, in either direction:
    - A missing key would leave that provider unbound, raising errors downstream.
    - An unknown key indicates an incomplete rename that would bind nothing.
    - A pipeline key would replace a test's fake fetcher or model call on every
      backend switch.

    Args:
        bindings: Mapping of provider names to factory callables.

    Returns:
        Mapping of each provider name in `bindings` to what it held before.

    Raises:
        ValueError: If `bindings` misses a backend provider, or names a
            pipeline provider or an unknown one.
    """
    backend_providers = set(PROVIDERS) - PIPELINE_PROVIDERS
    missing = backend_providers - set(bindings)
    extra = set(bindings) - backend_providers
    if missing or extra:
        raise ValueError(
            f"backend does not match the provider list; missing={sorted(missing)}, "
            f"unknown or pipeline={sorted(extra)}"
        )
    return _swap_providers(bindings)


def _swap_providers(
    bindings: Mapping[str, Callable[..., Any]],
) -> dict[str, Any]:
    previous = {
        name: getattr(PROVIDERS[name].module, PROVIDERS[name].attribute)
        for name in bindings
    }
    for name, binding in bindings.items():
        setattr(PROVIDERS[name].module, PROVIDERS[name].attribute, binding)
    return previous


def _restore_providers(previous: Mapping[str, Any]) -> None:
    for name, binding in previous.items():
        setattr(PROVIDERS[name].module, PROVIDERS[name].attribute, binding)


def configure_providers(
    store: InMemoryStore | None = None,
) -> InMemoryStore | None:
    """Installs the configured storage backend.

    Called once by every entry point before anything accesses a provider.

    Args:
        store: Optional pre-existing InMemoryStore for standalone mode.

    Returns:
        The `InMemoryStore` instance in standalone mode, or None for deployments.
    """
    if config_module.load().standalone:
        store = store or standalone_bindings.create_store()
        jobs_dir = standalone_bindings.resolve_jobs_dir()
        bind_providers(
            standalone_bindings.build_bindings(store, jobs_dir=jobs_dir)
        )
        logger.info(
            "standalone: storage is in this process and under %s; nothing is "
            "written to BigQuery or Cloud Storage.",
            jobs_dir,
        )
        return store
    bind_providers(build_deployed_bindings())
    return None


@contextlib.contextmanager
def override_providers(
    *,
    standalone: bool = False,
    store: InMemoryStore | None = None,
    jobs_dir: Path | None = None,
) -> Iterator[InMemoryStore | None]:
    """Temporarily overrides provider bindings within a context block.

    Restores previous provider targets upon exiting the context.

    Args:
        standalone: Whether to bind the standalone backend.
        store: Optional existing InMemoryStore to use when standalone is True.
        jobs_dir: Directory for the standalone files; defaults to
            `standalone.files.JOBS_DIR` under the working directory.

    Yields:
        The active InMemoryStore instance if standalone, or None for a deployment.
    """
    if standalone:
        store = store or standalone_bindings.create_store()
        previous = bind_providers(
            standalone_bindings.build_bindings(
                store,
                jobs_dir=jobs_dir or standalone_bindings.resolve_jobs_dir(),
            )
        )
    else:
        store = None
        previous = bind_providers(build_deployed_bindings())
    try:
        yield store
    finally:
        _restore_providers(previous)


@contextlib.contextmanager
def override_bindings(
    bindings: Mapping[str, Callable[..., Any]],
) -> Iterator[None]:
    """Binds the named providers within a context block, leaving the rest alone.

    Each provider gets back what it held on entry when the block exits, also
    when it exits with an exception. Providers are module attributes, so the
    bindings hold for the whole process, every thread and task included, and
    only one block may be open at a time: a second one, nested or concurrent,
    is refused rather than given back out of order.

    Args:
        bindings: Mapping of provider names in `PROVIDERS` to factory
            callables; any subset, pipeline providers included.

    Yields:
        None while the bindings are in place.

    Raises:
        ValueError: If `bindings` names a provider `PROVIDERS` does not;
            nothing is bound then.
        RuntimeError: If another `override_bindings` block is open; nothing
            is bound then.
    """
    unknown = set(bindings) - set(PROVIDERS)
    if unknown:
        raise ValueError(f"unknown provider(s): {sorted(unknown)}")
    if not _BINDINGS_HELD.acquire(blocking=False):
        raise RuntimeError(
            "override_bindings is already open in this process; providers are "
            "process-wide, so blocks may not nest or overlap"
        )
    try:
        previous = _swap_providers(bindings)
        try:
            yield
        finally:
            _restore_providers(previous)
    finally:
        _BINDINGS_HELD.release()
