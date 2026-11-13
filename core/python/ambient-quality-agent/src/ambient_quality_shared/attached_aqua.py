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

"""The record `attach` leaves in a project, `.aqua/attached_aqua.json`.

It holds what later `agents-cli aqua` commands in the project reuse, so that
the observed agent's own directory needs no flags:

- `aqua_resource`: the AQuA engine an `attach --aqua-resource` reached,
  written by the agents-cli extension;
- `agent_key` and `agent_key_target`: the attachment the last `attach` stored
  or changed, and the AQuA it is stored on, written by the CLI. A key names an
  attachment on one AQuA only, so the CLI selects it only when it reaches that
  AQuA.

Two writers share the file, so each updates only its own fields.

Standard library only: the agents-cli extension reads it with no installed
environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ATTACHED_AQUA_FILE = "attached_aqua.json"


def build_attached_aqua_path(state_root: Path) -> Path:
    """Returns the path of the record.

    Args:
        state_root: The project's state directory, `.aqua`.

    Returns:
        Path to `attached_aqua.json` in it.
    """
    return state_root / ATTACHED_AQUA_FILE


def load_attached_aqua(state_root: Path) -> dict[str, Any]:
    """Loads the record.

    Args:
        state_root: The project's state directory, `.aqua`.

    Returns:
        The record, or an empty dict if it is missing, unreadable or not a JSON
        object.
    """
    path = build_attached_aqua_path(state_root)
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers both non-UTF-8 bytes (UnicodeDecodeError) and
        # non-JSON text (JSONDecodeError).
        return {}
    return record if isinstance(record, dict) else {}


def save_attached_aqua_fields(state_root: Path, **fields: str | None) -> Path:
    """Sets fields of the record, keeping the ones the other writer owns.

    Args:
        state_root: The project's state directory, `.aqua`; created if missing.
        **fields: Values to set; None removes the field.

    Returns:
        The path written.

    Raises:
        OSError: If the record cannot be written.
    """
    record = load_attached_aqua(state_root)
    for name, value in fields.items():
        if value is None:
            record.pop(name, None)
        else:
            record[name] = value
    path = build_attached_aqua_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def normalize_target(target: str) -> str:
    """Normalizes an AQuA target for comparison.

    Args:
        target: An engine resource name or URL.

    Returns:
        The target without surrounding whitespace or trailing slashes.
    """
    return target.strip().rstrip("/")


def resolve_recorded_agent_key(state_root: Path, target: str) -> str:
    """Resolves the agent key recorded for the AQuA a command reaches.

    Args:
        state_root: The project's state directory, `.aqua`.
        target: The AQuA engine resource name or URL the command reaches.

    Returns:
        The recorded key if it was recorded for `target`, else an empty string.
    """
    record = load_attached_aqua(state_root)
    key = record.get("agent_key")
    recorded_target = record.get("agent_key_target")
    if not isinstance(key, str) or not isinstance(recorded_target, str):
        return ""
    if not target or normalize_target(recorded_target) != normalize_target(
        target
    ):
        return ""
    return key
