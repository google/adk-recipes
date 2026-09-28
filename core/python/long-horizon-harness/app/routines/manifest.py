# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Routine manifest: the declarative definition of a scheduled routine task,
plus the backend-side writer that persists it.

One YAML file under ``.lha/routines/<id>.yaml``. The id is the file stem; the
body declares name/schedule/task plus the scoped secrets and delivery. The
declared ``secrets`` list IS the routine's blast-radius boundary — a routine can
only ever hold the secrets it names. The runtime copy the fire path reads is the
RoutineStore row (``scheduler/routine_store.py``), not these YAML files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

DEFAULT_DELIVERY = "report_back"
ROUTINE_OVERLAY_DIR = ".lha/routines"


@dataclass(frozen=True)
class RoutineManifest:
    id: str
    name: str
    schedule: str
    task: str
    secrets: tuple[str, ...]
    delivery: str


def _nonempty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(
            v.strip() for v in value if isinstance(v, str) and v.strip()
        )
    return ()


def parse_manifest(obj: Any, *, routine_id: str) -> RoutineManifest | None:
    """Normalize a manifest body dict → RoutineManifest, or None if invalid."""
    if not isinstance(obj, dict):
        return None
    name = _nonempty_str(obj.get("name"))
    schedule = _nonempty_str(obj.get("schedule"))
    task = _nonempty_str(obj.get("task"))
    if not (name and schedule and task and routine_id):
        return None
    return RoutineManifest(
        id=routine_id,
        name=name,
        schedule=schedule,
        task=task,
        secrets=_str_tuple(obj.get("secrets")),
        delivery=_nonempty_str(obj.get("delivery")) or DEFAULT_DELIVERY,
    )


async def write_routine_via_env(
    env, body: dict, *, routine_id: str
) -> RoutineManifest:
    """Validate + persist a manifest to ``.lha/routines/<id>.yaml`` through the
    environment interface, so it works under both local and sandbox backends.

    ``env`` is a BaseEnvironment (e.g. ``active_environment()``). The agent never
    writes ``.lha/`` itself — this runs backend-side on user approval.
    """
    manifest = parse_manifest(body, routine_id=routine_id)
    if manifest is None:
        raise ValueError(f"invalid routine manifest for {routine_id!r}")
    path = env.working_dir / ROUTINE_OVERLAY_DIR / f"{routine_id}.yaml"
    text = yaml.safe_dump(body, sort_keys=False)
    await env.write_file(path, text)
    return manifest
