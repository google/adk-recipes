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

"""The developer's goal for the observed agent, and every version of it.

`goal.md` is the active goal. Every save also keeps the text as an immutable
version, ``goals/<id>.json``, whose id is the hash of the text alone, and
appends an activation record, ``goals/activations/<time>-<id>.json``. The same
text is always the same version, so restoring an earlier goal is saving it
again, and the activation log says which version was active when. Saving an
empty goal removes it and logs an activation of no version. Timestamps
are written here rather than taken from GCS object metadata, which records no
re-activation and does not exist in standalone runs, the mock or the tests.

The save path is the same whoever calls it -- the dashboard route, the chat's
`set_goal` once the user approves, a CLI or the harness later -- because it
lives here. Every investigation reads it once, through
`load_investigation_goal`.

Every function takes the `ObjectStore` the documents live in: the jobs bucket in
a deployment, `.aqua/job/` in a standalone run.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ambient_quality_agent.tools.objects.store import ObjectStore

logger = logging.getLogger(__name__)

GOAL_OBJECT = "goal.md"
"""The active goal, at the root of the jobs store."""

VERSIONS_PREFIX = "goals/"
ACTIVATIONS_PREFIX = "goals/activations/"

GOAL_MAX_BYTES = 8 * 1024
"""UTF-8 bytes a goal may hold; a longer one is refused on save, not cut."""

MAX_LISTED_VERSIONS = 50
"""Versions `list_versions` returns, most recently active first; each one is a
read."""

_ACTIVATION_STAMP = "%Y%m%dT%H%M%S%fZ"
"""The time at the start of an activation's object name, which sorts as text."""


def compute_goal_version(text: str) -> str:
    """Computes the version identifier of a goal text.

    Args:
        text: Raw goal text.

    Returns:
        First 12 hex characters of the SHA-256 digest of the stripped text.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def read_goal(store: ObjectStore) -> str | None:
    """Reads the active goal.

    Args:
        store: Where the documents live.

    Returns:
        Stripped goal text, or None if no goal is stored or if it is empty.
    """
    text = store.read_text(GOAL_OBJECT)
    return (text.strip() or None) if text is not None else None


def save_goal(store: ObjectStore, text: str) -> str:
    """Persists text as the active goal and records its version history.

    Empty text removes the goal: investigations then run without one. The
    removed text stays a version, so it can be restored. Text exceeding
    `GOAL_MAX_BYTES` is refused rather than truncated.

    Writes proceed in the following order:
    1. Write the version object (`goals/<id>.json`), without overwriting existing
       versions. Skipped when removing the goal.
    2. Write the activation record (`goals/activations/<timestamp>-<id>.json`),
       with `none` as the id and a null version when removing the goal.
    3. Update the active goal object (`goal.md`); empty when removing it.

    Args:
        store: Where the documents live.
        text: Goal content to save; empty to remove the goal.

    Returns:
        Cleaned goal text that was stored; empty when the goal was removed.

    Raises:
        ValueError: If text exceeds `GOAL_MAX_BYTES`.
    """
    cleaned = (text or "").strip()
    if len(cleaned.encode("utf-8")) > GOAL_MAX_BYTES:
        raise ValueError(
            f"The goal is longer than {GOAL_MAX_BYTES // 1024} KiB; shorten it."
        )
    now = dt.datetime.now(dt.UTC)
    version = _save_goal_version(store, cleaned, now) if cleaned else None
    store.write_text(
        f"{ACTIVATIONS_PREFIX}{now.strftime(_ACTIVATION_STAMP)}-{version or 'none'}.json",
        json.dumps({"version": version, "activated_at": now.isoformat()})
        + "\n",
    )
    store.write_text(GOAL_OBJECT, cleaned)
    return cleaned


def _save_goal_version(store: ObjectStore, text: str, now: dt.datetime) -> str:
    """Stores a goal text as its version, unless that version already exists.

    Args:
        store: Where the documents live.
        text: Stripped goal text.
        now: Time recorded as the version's first save if it is new.

    Returns:
        The version id.
    """
    version = compute_goal_version(text)
    store.write_text(
        f"{VERSIONS_PREFIX}{version}.json",
        json.dumps({"text": text, "created_at": now.isoformat()}, indent=2)
        + "\n",
        overwrite=False,
    )
    return version


@dataclasses.dataclass(frozen=True)
class GoalVersion:
    """One saved goal text, with when it was first saved and last made active."""

    version: str
    text: str
    created_at: str
    """ISO 8601; when the text was first saved."""

    last_activated_at: str
    """ISO 8601; when a save last made this text the active goal. Empty when
    none did: a goal saved before versions existed, stored when an
    investigation read it."""


_NEVER_ACTIVATED = dt.datetime.min.replace(tzinfo=dt.UTC)


def list_versions(store: ObjectStore) -> list[GoalVersion]:
    """Lists the saved versions, most recently active first.

    Versions never made active come after every version that was. A version
    or an activation whose object does not parse is skipped with a warning,
    costing only itself.

    Args:
        store: Where the documents live.

    Returns:
        At most `MAX_LISTED_VERSIONS` versions.
    """
    names = store.list_names(VERSIONS_PREFIX)
    # Activation names carry their time and version, so the order comes from
    # one listing and only the versions returned are read.
    last_active: dict[str, dt.datetime] = {}
    for name in names:
        if not name.startswith(ACTIVATIONS_PREFIX):
            continue
        stamp, _, rest = name.removeprefix(ACTIVATIONS_PREFIX).partition("-")
        try:
            when = dt.datetime.strptime(stamp, _ACTIVATION_STAMP).replace(
                tzinfo=dt.UTC
            )
        except ValueError:
            logger.warning(
                "goal: skipping activation %s, whose name does not parse.", name
            )
            continue
        version = rest.removesuffix(".json")
        last_active[version] = max(
            when, last_active.get(version, _NEVER_ACTIVATED)
        )

    ids = [
        name.removeprefix(VERSIONS_PREFIX).removesuffix(".json")
        for name in names
        if name.endswith(".json")
        and "/" not in name.removeprefix(VERSIONS_PREFIX)
    ]
    ids.sort(key=lambda v: last_active.get(v, _NEVER_ACTIVATED), reverse=True)

    versions: list[GoalVersion] = []
    for version in ids[:MAX_LISTED_VERSIONS]:
        raw = store.read_text(f"{VERSIONS_PREFIX}{version}.json")
        if raw is None:
            continue
        try:
            data = json.loads(raw)
            text, created_at = (
                str(data["text"]),
                str(data.get("created_at") or ""),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning(
                "goal: skipping version %s, which does not parse: %s",
                version,
                exc,
            )
            continue
        activated = last_active.get(version)
        versions.append(
            GoalVersion(
                version=version,
                text=text,
                created_at=created_at,
                last_activated_at=activated.isoformat() if activated else "",
            )
        )
    return versions


@dataclasses.dataclass(frozen=True)
class InvestigationGoal:
    """The goal one investigation reviews with, and where it came from."""

    text: str = ""
    """The goal as every review prompt receives it; empty for no goal."""

    source: str = "unset"
    """``saved`` when read from goal.md; ``unset`` when none is saved;
    ``unreadable`` when goal.md could not be read or is over `GOAL_MAX_BYTES`,
    so the run went without the goal the developer may have written; ``file``
    when the quality harness supplied it."""

    @property
    def version(self) -> str | None:
        """The version id of `text`, or `None` for no goal."""
        return compute_goal_version(self.text) if self.text else None


def load_investigation_goal(store: ObjectStore) -> InvestigationGoal:
    """Loads the goal an investigation reviews with; never raises.

    A goal that cannot be read gives no goal, recorded as ``unreadable``, so the
    investigation still runs. A goal saved before versions existed is stored as
    its version here, so the version the investigation records can be looked
    up; failing to store it is logged and the goal is still used.

    Args:
        store: Where the documents live.

    Returns:
        The goal and where it came from.
    """
    try:
        text = read_goal(store)
    except Exception:
        logger.exception(
            "goal: could not read the goal; reviewing without one."
        )
        return InvestigationGoal(source="unreadable")
    if text is None:
        return InvestigationGoal()
    if len(text.encode("utf-8")) > GOAL_MAX_BYTES:
        logger.warning(
            "goal: goal.md is longer than %d KiB, which a save refuses; "
            "reviewing without it.",
            GOAL_MAX_BYTES // 1024,
        )
        return InvestigationGoal(source="unreadable")
    try:
        _save_goal_version(store, text, dt.datetime.now(dt.UTC))
    except Exception as exc:
        logger.warning(
            "goal: could not store version %s: %s",
            compute_goal_version(text),
            exc,
        )
    return InvestigationGoal(text=text, source="saved")


def build_goal_uri(store: ObjectStore) -> str:
    return store.uri(GOAL_OBJECT)
