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

"""The agent key: what names one attachment of an observed agent.

The key is `<project id>__<agent name>`: the project the agent runs in and its
telemetry is read from, and the root agent name ADK gives it. The same agent
deployed into a staging and a prod project gets two keys, so both can be
attached to one AQuA.

The key names the attachment: its stored object, its update sink, and the
value a request names the attachment by. The
agent name alone remains the telemetry filter.

An agent in AQuA's own project keeps its bare name in all of those places, so
what is stored under that name stays where it is. A bare name that reads as a
key is not kept (`observed_agent_config.keys`), so a bare name and a key never
collide, and the agent's copies in other projects can be attached beside it.

Standard library only: the CLI, the agents-cli extension and the engine all
read it.
"""

from __future__ import annotations

import re

SEPARATOR = "__"
"""Between the project id and the agent name.

The key goes into the attachment's object name and the update sink's name
(`<topic>-<key>-updates`, cut and hashed past 100 characters). A log sink name
allows only letters, digits, `-`, `_` and `.`, so a separator such as `:` would
need a second spelling of every key.

`__` is unambiguous: a project id holds no underscore, so the first occurrence
is the boundary, and an agent name, a Python identifier, may hold any number of
underscores after it (`acme-prod__it__support` is the agent `it__support`)."""

AGENT_KEY_RE = re.compile(
    r"^(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])__(?P<agent>[A-Za-z_][A-Za-z0-9_]*)$"
)
"""A project id (6 to 30 characters, as Cloud projects allow) and a Python
identifier, which ADK requires of an agent name."""

STORAGE_KEY_PATTERN = (
    r"^([a-z][a-z0-9-]{4,28}[a-z0-9]__)?[A-Za-z_][A-Za-z0-9_]*$"
)
"""What an attachment can be stored under: an agent key, or the bare agent name
an agent in AQuA's own project keeps. `ambient_quality_cli.update_trigger`
names an update sink only for a key that matches it."""


def build_agent_key(project_id: str, agent_name: str) -> str:
    """Builds the key of an agent from its project and its name.

    Args:
        project_id: Project the agent runs in, by id.
        agent_name: The agent's root agent name.

    Returns:
        The key, as `<project id>__<agent name>`. It is not validated:
        `is_storage_key` says whether it is well formed.
    """
    return f"{project_id}{SEPARATOR}{agent_name}"


def is_storage_key(value: str) -> bool:
    """Checks whether a value is something an attachment can be stored under.

    Args:
        value: Candidate key.

    Returns:
        True if `value` is an agent key or a bare agent name.
    """
    return re.fullmatch(STORAGE_KEY_PATTERN, value) is not None


def extract_agent_name(agent_key: str) -> str:
    """Extracts the agent name from a key.

    Args:
        agent_key: An agent key, or a bare agent name.

    Returns:
        The agent name part of a well-formed key, otherwise `agent_key`
        itself.
    """
    match = AGENT_KEY_RE.fullmatch(agent_key)
    return match["agent"] if match else agent_key
