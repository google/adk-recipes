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

"""Which key an agent's attachment and update sink are named by.

An agent key is `<project id>__<agent name>` (`ambient_quality_shared.agent_key`).
An agent in AQuA's own project keeps its bare name instead, so that an
attachment and update sink named by it stay where they are. The
choice depends on what is stored:

1. An attachment under the agent key wins.
2. Otherwise an attachment under the bare name, for the same project, wins.
3. Otherwise an agent in AQuA's own project keeps its bare name, unless the
   bare name holds another project's agent, or reads as a key itself
   (`support__bot` would be agent `bot` of project `support`).
4. Otherwise the agent key.

A bare name kept by rule 3 never reads as a key, so it cannot collide with
one, and the same agent in another project gets its own key beside it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ambient_quality_shared.agent_key import AGENT_KEY_RE, build_agent_key

if TYPE_CHECKING:
    from ambient_quality_agent.config import Config
    from ambient_quality_agent.tools.observed_agent_config.store import (
        AgentRecord,
    )


def resolve_key_project(observed_project_id: str, config: Config) -> str:
    """Resolves the project an agent's key names, by id.

    Args:
        observed_project_id: The agent's `observed_project_id`; empty, or
            AQuA's own project by number, both mean AQuA's project.
        config: AQuA's deployment configuration.

    Returns:
        The agent's project, with AQuA's own given by id.
    """
    if observed_project_id in {"", config.project_id}:
        return config.resolve_aqua_project_id()
    return observed_project_id


def resolve_stored_key(
    agent_name: str,
    project_id: str,
    *,
    config: Config,
    lookup: Callable[[str], AgentRecord | None],
) -> str:
    """Chooses what an agent is stored under, by the rules of this module.

    Args:
        agent_name: The agent's root agent name.
        project_id: The project it runs in, by id.
        config: AQuA's deployment configuration.
        lookup: Reads the attachment stored under a key, or None.

    Returns:
        The agent key, or the bare agent name.
    """
    key = build_agent_key(project_id, agent_name)
    if lookup(key) is not None:
        return key
    unkeyed = lookup(agent_name)
    if unkeyed is not None and (
        resolve_key_project(unkeyed.config.observed_project_id, config)
        == project_id
    ):
        return agent_name
    if (
        unkeyed is None
        and project_id == config.resolve_aqua_project_id()
        and not AGENT_KEY_RE.fullmatch(agent_name)
    ):
        return agent_name
    return key
