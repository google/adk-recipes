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

The configuration combines AQuA's own settings, from the environment, with the
observed agent's configuration stored via `agents-cli aqua attach` (`store`) in
the jobs store (`objects.store.jobs_store_factory`).

A request selects its agent by the state key `AGENT_KEY_STATE`, holding what
the agent is stored under: its agent key (`ambient_quality_shared.agent_key`),
or the bare name an agent in AQuA's own project keeps (`keys`). A key that
names no attached agent is an error rather than a fallback, so a request never
reads one agent's data under another's key. A request that selects none gets
the default attached agent (`store.resolve_default`). A deployment has nothing
to investigate until one is attached. `Config.agent_key` says what the resolved agent is stored
under.

Ambient triggers select their agent the same way. A scheduler tick is for
every agent `list_agent_keys` returns, and an observed engine's update is for
the agent `resolve_engine_agent_key` finds by the engine's id.

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
from ambient_quality_shared import engine_agent

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0
"""Duration in seconds to reuse cached agent configuration reads.

Runs snapshot configuration upon reading, making staleness negligible. The cache
primarily serves the `/config` route, which receives frequent dashboard requests
relative to agent re-attachment frequency.
"""

AGENT_KEY_STATE = "aqa_agent_key"
"""The state key a request selects its agent by. Prefixed, unlike the flat
configuration a run's workflow state holds, whose `agent_key` it must not
shadow."""

_cache: dict[str, tuple[float, store.AgentRecord | None]] = {}

# The default attached agent. Resolving it reads every attached object, so it is
# cached like a record.
_default_cache: list[tuple[float, str | None]] = []


class UnknownAgentError(LookupError):
    """A request selected an agent key that names no usable attachment."""


def clear_cache() -> None:
    """Drops cached agent configuration reads.

    Intended for tests and processes needing to read back newly written configuration.
    """
    _cache.clear()
    _default_cache.clear()


def forget_cached_record(agent_key: str) -> None:
    """Drops the cached read for one agent after its object is written or deleted.

    Only this process's cache is affected. Another replica keeps its entry until
    `CACHE_TTL_SECONDS` elapse, which bounds how long an attach takes to reach
    every request.

    Args:
        agent_key: Key of the observed agent.
    """
    _cache.pop(agent_key, None)
    # Attaching or detaching any agent can change which one is the default.
    _default_cache.clear()


def load(state: ADKStateLike) -> Config:
    """Resolves the effective `Config` for the current request.

    Args:
        state: Session state; `AGENT_KEY_STATE` in it selects the agent.

    Returns:
        Effective `Config` combining AQuA's own and the attached agent's
        settings.

    Raises:
        UnknownAgentError: If the state selects an agent that is not attached,
            or whose stored configuration cannot be read or is not valid.
    """
    agent_key = _extract_selected_agent_key(state)
    if agent_key:
        return load_selected_agent_config(env_config, agent_key)
    return apply_attached_agent_config(env_config)


def list_agent_keys() -> list[str]:
    """Lists every agent the deployment investigates on its own triggers.

    Returns:
        Every attached agent's key, alphabetically.

    Raises:
        Exception: Whatever listing the attachments raised. Read as none, a
            failed listing would skip every agent without a trace.
    """
    try:
        return store.list_agents(objects.jobs_store_factory())
    except objects.StorageNotConfiguredError:
        return []


def resolve_engine_agent_key(engine_id: str) -> str | None:
    """Resolves the agent an update of the observed engine `engine_id` is for.

    Gathers what `engine_agent.resolve_engine_agent_key` decides from, so an
    update is investigated against the agent `publish-source` files the
    engine's source under. Each attachment, and the default attached agent,
    is read afresh rather than from the cache, which holds a failed read as a
    missing record and would hand the update to the wrong agent or to none.

    Args:
        engine_id: Trailing id of the observed engine's resource name.

    Returns:
        The agent's key, or `None` when no agent can be told to be the
        engine's.

    Raises:
        Exception: Whatever listing or reading the attachments raised, so that
            the update is delivered again rather than given to another agent.
    """
    jobs = objects.jobs_store_factory()
    recorded_engines: dict[str, str] = {}
    for agent_key in list_agent_keys():
        try:
            record = store.get_agent_record(jobs, agent_key)
        except objects.StorageNotConfiguredError:
            record = None
        recorded_engines[agent_key] = (
            record.observed_engine_id if record else ""
        )
    default_key = ""
    try:
        default_key = store.resolve_default(jobs) or ""
    except objects.StorageNotConfiguredError:
        pass
    return (
        engine_agent.resolve_engine_agent_key(
            engine_id,
            recorded_engines,
            default_key,
            aqua_engine_id=env_config.aqa_engine_id,
        )
        or None
    )


def _extract_selected_agent_key(state: ADKStateLike) -> str:
    """Extracts the agent a request selects from its state.

    Args:
        state: Session state.

    Returns:
        The key in `AGENT_KEY_STATE`, or empty when the request selects none.
    """
    return str(state.get(AGENT_KEY_STATE) or "").strip()


def load_selected_agent_config(config: Config, agent_key: str) -> Config:
    """Layers one selected agent's stored configuration over `config`.

    Args:
        config: Base deployment configuration.
        agent_key: Key of the agent the request selects.

    Returns:
        `config` with the agent's settings layered on.

    Raises:
        UnknownAgentError: If no attachment can be read under `agent_key`, or
            its stored configuration is not valid.
    """
    return _layer_agent_record(
        config, agent_key, _get_attached_agent_record(agent_key)
    )


def load_fresh_agent_config(agent_key: str) -> Config:
    """Resolves one agent's effective `Config` from a fresh read of its record.

    For a trigger that is delivered again when it raises: the cache holds a
    failed read as a missing record, which would answer the trigger as being
    for an agent that is not attached, and drop it. The fresh record replaces
    the cached one, so the investigation the trigger starts reads it too.

    Args:
        agent_key: Key of the agent.

    Returns:
        AQuA's configuration with the agent's settings layered on.

    Raises:
        UnknownAgentError: If nothing is attached under `agent_key`, or its
            stored configuration is not valid.
        Exception: Whatever reading or parsing the stored object raised.
    """
    record = None
    try:
        record = store.get_agent_record(objects.jobs_store_factory(), agent_key)
    except objects.StorageNotConfiguredError:
        pass
    _cache[agent_key] = (time.monotonic() + CACHE_TTL_SECONDS, record)
    return _layer_agent_record(env_config, agent_key, record)


def _layer_agent_record(
    config: Config, agent_key: str, record: store.AgentRecord | None
) -> Config:
    """Layers an agent's stored record over `config`.

    Args:
        config: Base deployment configuration.
        agent_key: Key the record is stored under.
        record: The stored record; None when none could be read.

    Returns:
        `config` with the agent's settings layered on.

    Raises:
        UnknownAgentError: If there is no record, or the record's configuration
            is not valid.
    """
    if record is None:
        raise UnknownAgentError(
            f"No agent is attached as {agent_key!r}, or its attachment cannot "
            "be read. `agents-cli aqua list-agents` lists the attached agents."
        )
    try:
        return config.copy_with_agent(record.config)
    except ValueError as exc:
        raise UnknownAgentError(
            f"The stored configuration for {agent_key!r} is not valid: {exc}"
        ) from exc


def resolve_default_agent_key() -> str | None:
    """Resolves the agent a request that selects none is about.

    Returns:
        The default attached agent's key, or `None` when none is attached or
        the attachments cannot be read.
    """
    now = time.monotonic()
    # A slice, because another thread may clear the cache between a check and
    # an index.
    cached = _default_cache[:1]
    if cached and cached[0][0] > now:
        return cached[0][1]

    key = None
    jobs = objects.jobs_store_factory()
    try:
        key = store.resolve_default(jobs)
    except objects.StorageNotConfiguredError:
        pass
    except Exception:
        logger.warning(
            "could not resolve the default attached agent from %s.",
            jobs.uri(store.AGENTS_PREFIX),
            exc_info=True,
        )
    _default_cache[:] = [(now + CACHE_TTL_SECONDS, key)]
    return key


def apply_attached_agent_config(config: Config) -> Config:
    """Layers the default agent's stored configuration over `config`.

    Falls back to `config` if reading fails or no stored configuration exists,
    logging warnings on failure rather than aborting the request.

    Args:
        config: Base deployment configuration.

    Returns:
        Updated `Config` with attached agent settings layered on, or `config`,
        whose `observed_agent_name` is empty, when no agent is attached or its
        stored configuration cannot be used.
    """
    agent_key = resolve_default_agent_key()
    if not agent_key:
        return config
    record = _get_attached_agent_record(agent_key)
    if record is None:
        return config
    try:
        return config.copy_with_agent(record.config)
    except ValueError:
        logger.warning(
            "the stored configuration for %s is not valid; no agent is "
            "selected.",
            agent_key,
            exc_info=True,
        )
        return config


def resolve_selected_agent_key(state: ADKStateLike) -> str | None:
    """Resolves the key of the agent a request is about.

    Args:
        state: Session state; `AGENT_KEY_STATE` in it selects the agent.

    Returns:
        The key the request selects, else the default agent's
        (`resolve_default_agent_key`); `None` when it selects none and there
        is no default agent.
    """
    return _extract_selected_agent_key(state) or resolve_default_agent_key()


def load_selected_agent_record(state: ADKStateLike) -> store.AgentRecord | None:
    """Loads the stored record of the agent a request is about.

    Read afresh rather than from the cache, which holds a failed read as a
    missing record: a caller acting on the schedule must not mistake a read
    that failed for an agent with no attachment, and run it on the default.

    Args:
        state: Session state; `AGENT_KEY_STATE` in it selects the agent, else
            the default agent is loaded.

    Returns:
        The record, or `None` when there is no such agent, it has no
        attachment, or the deployment has nowhere to keep attachments.

    Raises:
        Exception: Whatever reading or parsing the stored object raised.
    """
    agent_key = resolve_selected_agent_key(state)
    if not agent_key:
        return None
    try:
        return store.get_agent_record(objects.jobs_store_factory(), agent_key)
    except objects.StorageNotConfiguredError:
        return None


def _get_attached_agent_record(agent_key: str) -> store.AgentRecord | None:
    """Retrieves the stored agent record, returning a cached entry when valid.

    Failed reads are cached like missing records to limit read attempts to one per
    TTL window rather than once per request. A deployment with nowhere to keep
    records reads as having none, without a warning.

    Args:
        agent_key: Key of the observed agent.

    Returns:
        The `AgentRecord` if found and readable, or `None` otherwise.
    """
    now = time.monotonic()
    cached = _cache.get(agent_key)
    if cached is not None and cached[0] > now:
        return cached[1]

    record = None
    jobs = objects.jobs_store_factory()
    try:
        record = store.get_agent_record(jobs, agent_key)
    except objects.StorageNotConfiguredError:
        pass
    except Exception:
        logger.warning(
            "could not read the stored configuration for %s from %s.",
            agent_key,
            jobs.uri(store.build_object_name(agent_key)),
            exc_info=True,
        )
    _cache[agent_key] = (now + CACHE_TTL_SECONDS, record)
    return record


def serialize_config(config: Config) -> dict[str, Any]:
    """Serializes configuration into a JSON-safe dictionary.

    Args:
        config: Configuration instance to serialize.

    Returns:
        Dictionary of primitive configuration values suitable for `json.dumps`.
    """
    return dataclasses.asdict(config)
