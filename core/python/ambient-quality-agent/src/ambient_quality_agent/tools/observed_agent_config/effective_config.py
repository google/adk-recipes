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

"""The configuration a request runs under.

`load` resolves runtime configuration for all read points:
durable run snapshots, ambient trigger revision delay checks, and the
sessionless `/config` route read by UI and CLI.

The configuration combines deploy-time environment settings with the observed
agent's configuration stored via `agents-cli aqua attach` (`store`) in the jobs
store (`objects.store.jobs_store_factory`). When no stored configuration exists,
environment settings provide all values.

The agent is the one the environment names. A deployment whose environment
names none investigates the default attached agent (`store.resolve_default`),
and has nothing to investigate until one is attached.

Configuration has exactly one writer, `agents-cli aqua attach`, which sends its
changes to the command routes backed by `commands`. Nothing in a conversation
changes it, so run configuration is independent of the scheduling session.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from ambient_quality_agent.config import Config
from ambient_quality_agent.config import config as env_config
from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.observed_agent_config import store

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0
"""Duration in seconds to reuse cached agent configuration reads.

Runs snapshot configuration upon reading, making staleness negligible. The cache
primarily serves the `/config` route, which receives frequent dashboard requests
relative to agent re-attachment frequency.
"""

_cache: dict[str, tuple[float, store.AgentRecord | None]] = {}

# The default attached agent, for a deployment whose environment names none.
# Resolving it reads every attached object, so it is cached like a record.
_default_cache: list[tuple[float, str | None]] = []


def clear_cache() -> None:
    """Drops cached agent configuration reads.

    Intended for tests and processes needing to read back newly written configuration.
    """
    _cache.clear()
    _default_cache.clear()


def forget_cached_record(agent_name: str) -> None:
    """Drops the cached read for one agent after its object is written or deleted.

    Only this process's cache is affected. Another replica keeps its entry until
    `CACHE_TTL_SECONDS` elapse, which bounds how long an attach takes to reach
    every request.

    Args:
        agent_name: Name of the observed agent.
    """
    _cache.pop(agent_name, None)
    # Attaching or detaching any agent can change which one is the default.
    _default_cache.clear()


def load(state: ADKStateLike) -> Config:
    """Resolves the effective `Config` for the current request.

    Args:
        state: Session state object. Currently unused; retained for caller
            compatibility and future per-request agent resolution.

    Returns:
        Effective `Config` combining environment and attached agent settings.
    """
    del state  # Unused; the agent is resolved per deployment, not per request.
    return apply_attached_agent_config(env_config)


def resolve_observed_agent_name(config: Config | None = None) -> str | None:
    """Resolves the agent the deployment investigates.

    Args:
        config: Deployment configuration whose environment may name the agent;
            the process's environment configuration if None.

    Returns:
        The agent the environment names; otherwise the default attached agent,
        or `None` when none is attached or the attachments cannot be read.
    """
    config = config or env_config
    if config.observed_agent_name:
        return config.observed_agent_name
    now = time.monotonic()
    # A slice, because another thread may clear the cache between a check and
    # an index.
    cached = _default_cache[:1]
    if cached and cached[0][0] > now:
        return cached[0][1]

    name = None
    jobs = objects.jobs_store_factory()
    try:
        name = store.resolve_default(jobs)
    except objects.StorageNotConfiguredError:
        pass
    except Exception:
        logger.warning(
            "could not resolve the default attached agent from %s.",
            jobs.uri(store.AGENTS_PREFIX),
            exc_info=True,
        )
    _default_cache[:] = [(now + CACHE_TTL_SECONDS, name)]
    return name


def apply_attached_agent_config(config: Config) -> Config:
    """Layers the attached agent's stored configuration over `config`.

    Falls back to `config` if reading fails or no stored configuration exists,
    logging warnings on failure rather than aborting the request.

    Args:
        config: Base deployment configuration.

    Returns:
        Updated `Config` with attached agent settings layered on, or the original
        `config` if no valid stored record is available. With no agent named by
        the environment and none attached, `observed_agent_name` stays empty.
    """
    agent_name = resolve_observed_agent_name(config)
    if not agent_name:
        return config
    record = _get_attached_agent_record(agent_name)
    if record is None:
        return config
    try:
        return config.copy_with_agent(record.config)
    except ValueError:
        logger.warning(
            "the stored configuration for %s is not valid; using the "
            "environment's instead.",
            agent_name,
            exc_info=True,
        )
        return config


def _get_attached_agent_record(agent_name: str) -> store.AgentRecord | None:
    """Retrieves the stored agent record, returning a cached entry when valid.

    Failed reads are cached like missing records to limit read attempts to one per
    TTL window rather than once per request. A deployment with nowhere to keep
    records reads as having none, without a warning.

    Args:
        agent_name: Name of the observed agent.

    Returns:
        The `AgentRecord` if found and readable, or `None` otherwise.
    """
    now = time.monotonic()
    cached = _cache.get(agent_name)
    if cached is not None and cached[0] > now:
        return cached[1]

    record = None
    jobs = objects.jobs_store_factory()
    try:
        record = store.get_agent_record(jobs, agent_name)
    except objects.StorageNotConfiguredError:
        pass
    except Exception:
        logger.warning(
            "could not read the stored configuration for %s from %s; "
            "using the environment's instead.",
            agent_name,
            jobs.uri(store.build_object_name(agent_name)),
            exc_info=True,
        )
    _cache[agent_name] = (now + CACHE_TTL_SECONDS, record)
    return record


def serialize_config(config: Config) -> dict[str, Any]:
    """Serializes configuration into a JSON-safe dictionary.

    Args:
        config: Configuration instance to serialize.

    Returns:
        Dictionary of primitive configuration values suitable for `json.dumps`.
    """
    return dataclasses.asdict(config)
