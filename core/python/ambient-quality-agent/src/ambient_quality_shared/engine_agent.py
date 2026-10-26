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

"""Which attached agent an observed engine belongs to.

Two callers ask, and must answer alike: the engine, when an update of an
observed engine arrives, and `publish-source`, when it files an engine's source
under an agent's key. Answering differently would investigate one agent's
update against another agent's code.

Standard library only: the CLI and the engine both read it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)


def extract_engine_id(engine: str) -> str:
    """Extracts an engine's id from its id or its full resource name.

    Args:
        engine: An engine id, a resource name, or empty.

    Returns:
        The trailing path segment; empty for an empty `engine`.
    """
    return engine.rstrip("/").rsplit("/", 1)[-1]


def _list_engine_agent_keys(
    engine_id: str, recorded_engines: Mapping[str, str]
) -> list[str]:
    """Lists the agents whose attachment records the engine `engine_id`.

    Args:
        engine_id: Trailing id of the engine's resource name.
        recorded_engines: Each attached agent's key, mapped to the engine its
            attachment records, an id or a resource name; empty for none.

    Returns:
        The matching keys, in the order of `recorded_engines`.
    """
    return [
        agent_key
        for agent_key, recorded in recorded_engines.items()
        if recorded and extract_engine_id(recorded) == engine_id
    ]


def resolve_engine_agent_key(
    engine_id: str,
    recorded_engines: Mapping[str, str],
    default_key: str,
    *,
    aqua_engine_id: str = "",
) -> str:
    """Resolves the agent the engine `engine_id` belongs to.

    1. No agent for AQuA's own engine: an update of AQuA is not an agent's.
    2. The first agent whose attachment records the engine. Several doing so
       is logged, since only the first is chosen.
    3. Otherwise the default agent, unless its attachment records another
       engine. An agent attached without an engine id has its engine only
       in its own deployment, so it may well be this one.

    Args:
        engine_id: Trailing id of the engine's resource name.
        recorded_engines: Each attached agent's key, mapped to the engine its
            attachment records, an id or a resource name; empty for none.
        default_key: Key of the agent a request that selects none gets; empty
            when there is none.
        aqua_engine_id: AQuA's own engine, an id or a resource name; empty
            when unknown.

    Returns:
        The agent's key, or empty when no agent can be told to be the
        engine's.
    """
    if aqua_engine_id and engine_id == extract_engine_id(aqua_engine_id):
        return ""
    matches = _list_engine_agent_keys(engine_id, recorded_engines)
    if len(matches) > 1:
        logger.warning(
            "%s all observe engine %s; it is taken to be %s's.",
            ", ".join(matches),
            engine_id,
            matches[0],
        )
    if matches:
        return matches[0]
    if recorded_engines.get(default_key):
        return ""
    return default_key
