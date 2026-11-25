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

"""Predicates and formatting for ongoing (in-flight or scheduled) investigations.

Shared by `/config` (`config_tools.show_config`) and `/health` (`health.assess`)
so every surface agrees on whether an investigation is actively running, queued,
or stalled.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

from ambient_quality_agent.tools.investigations.models import (
    IN_FLIGHT_STATUSES,
    SCHEDULED_INVESTIGATION_GRACE_MINUTES,
    STALE_IN_FLIGHT_MINUTES,
    InvestigationStatus,
)


def parse_instant(value: Any) -> dt.datetime | None:
    """Parses an ISO-8601 timestamp string into a UTC-aware datetime.

    Args:
        value: Timestamp string or arbitrary object.

    Returns:
        Parsed timezone-aware datetime, or None if `value` cannot be parsed.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _resolve_last_active_at(
    investigation: Mapping[str, Any],
) -> dt.datetime | None:
    """Resolves when an in-flight investigation last showed activity.

    Matches `ui/web/lib/investigation-state.ts`: takes the later of the start
    timestamp (`created_at`, falling back to `window_end`) and the newest stage
    heartbeat (`progress.updated_at`).

    Args:
        investigation: Formatted investigation mapping.

    Returns:
        Latest activity instant, or None when neither timestamp is present.
    """
    started = parse_instant(
        investigation.get("created_at") or investigation.get("window_end")
    )
    progress = investigation.get("progress")
    staged = (
        parse_instant(progress.get("updated_at"))
        if isinstance(progress, Mapping)
        else None
    )
    if staged is None:
        return started
    return staged if started is None else max(started, staged)


def is_ongoing_investigation(
    investigation: Mapping[str, Any], *, now: dt.datetime | None = None
) -> bool:
    """Checks whether an investigation is actively in flight or awaiting a due trigger.

    1. `pending` and `running` investigations count when active within
       `STALE_IN_FLIGHT_MINUTES` (or when undated, so an unparseable timestamp
       never hides an active sweep).
    2. `scheduled` investigations count until
       `SCHEDULED_INVESTIGATION_GRACE_MINUTES` past `due_at`.

    Args:
        investigation: Formatted investigation mapping.
        now: Reference UTC timestamp; defaults to current UTC time.

    Returns:
        True if the investigation is ongoing and not stale or past grace.
    """
    now = now or dt.datetime.now(tz=dt.UTC)
    status = str(investigation.get("status") or "").lower()
    if status in IN_FLIGHT_STATUSES:
        active = _resolve_last_active_at(investigation)
        if active is None:
            return True
        return (now - active) <= dt.timedelta(minutes=STALE_IN_FLIGHT_MINUTES)
    if status == InvestigationStatus.SCHEDULED.value:
        due = parse_instant(investigation.get("due_at"))
        if due is None:
            return True
        return now <= due + dt.timedelta(
            minutes=SCHEDULED_INVESTIGATION_GRACE_MINUTES
        )
    return False


def list_ongoing_investigations(
    investigations: Iterable[Mapping[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Filters investigations to ongoing ones, newest first.

    Args:
        investigations: Iterable of formatted investigation mappings.
        now: Reference UTC timestamp; defaults to current UTC time.

    Returns:
        Ongoing investigation dictionaries ordered newest first by start or due
        timestamp.
    """
    ref = now or dt.datetime.now(tz=dt.UTC)
    matched = [
        dict(inv)
        for inv in investigations
        if is_ongoing_investigation(inv, now=ref)
    ]
    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    return sorted(
        matched,
        key=lambda inv: (
            parse_instant(
                inv.get("created_at")
                or inv.get("due_at")
                or inv.get("window_end")
            )
            or epoch
        ),
        reverse=True,
    )


def format_ongoing_investigation(
    investigation: Mapping[str, Any],
) -> dict[str, Any]:
    """Formats an ongoing investigation into the compact summary exposed on `/config` and `/health`.

    Args:
        investigation: Formatted investigation mapping.

    Returns:
        Compact dictionary describing the ongoing investigation and its stage
        progress.
    """
    return {
        "investigation_id": str(investigation.get("investigation_id") or ""),
        "status": str(investigation.get("status") or ""),
        "trigger_type": str(investigation.get("trigger_type") or ""),
        "created_at": investigation.get("created_at"),
        "due_at": investigation.get("due_at"),
        "window_start": investigation.get("window_start"),
        "window_end": investigation.get("window_end"),
        "progress": investigation.get("progress"),
    }
