"""Append-only audit log for corrections and Legal-review resolutions.

PRD guardrail (§8): "Every correction is logged with who made it and when,
even though it's applied immediately -- so it stays auditable." This module
is intentionally separate from Memory Bank: Memory Bank stores the *current*
facts/rules an agent should retrieve, while the audit log stores the
immutable *history* of who changed what and why, for compliance review.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clause_agent.shared_libraries import config

_lock = threading.Lock()


def append_event(
    event_type: str, payload: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    """Appends a single audit event and returns the stored record.

    Args:
      event_type: Short event category, e.g. "correction" or
        "legal_review_resolved".
      payload: Event-specific fields (must be JSON-serializable).
      path: Override the audit log location (used by tests).

    Returns:
      The full record that was written, including `event_type` and
      `logged_at`.
    """
    record: dict[str, Any] = {
        "event_type": event_type,
        "logged_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    target = path or config.get_audit_log_path()
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    return record


def read_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Reads all audit events oldest-first. Used by tests and inspection."""
    target = path or config.get_audit_log_path()
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
