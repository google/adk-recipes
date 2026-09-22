"""Correction tool: structured intake for downstream (Billing) corrections.

Unlike a Legal-review ruling, a correction is *not* blocking (PRD glossary:
"Non-blocking correction (Business loop) -- a human corrects the agent's
answer after the fact. The agent doesn't need to stop and wait -- it just
accepts the fix and remembers it going forward."). It is applied
immediately, but always logged for auditability (PRD §8).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from clause_agent.shared_libraries import audit_log
from clause_agent.shared_libraries.schemas import Correction


def submit_correction(
    field: str,
    customer: str,
    wrong_answer: str,
    correct_value: str,
    correct_source: str,
    root_cause: str,
    reported_by: str,
    proposed_rule: str | None = None,
) -> dict[str, Any]:
    """Logs a structured correction from a downstream user.

    Call this whenever a Billing/AR analyst tells you an answer was wrong,
    *before* writing the corrected fact (and any generalizable rule) to
    Memory Bank via `memory_bank_create`. This keeps every correction
    traceable to who reported it and why, even though the fix is applied
    immediately without escalation.

    Args:
      field: The field that was wrong, e.g. "customer_id" or
        "payment_term".
      customer: Customer/company the correction applies to.
      wrong_answer: What the agent originally answered.
      correct_value: The correct value, per the human's verification.
      correct_source: Where the correct value actually lives, e.g.
        "2018 contract, Exhibit A".
      root_cause: Why the agent got it wrong, e.g. "search scope excluded
        exhibits".
      reported_by: Identity of the person reporting the correction.
      proposed_rule: An optional reusable rule to generalize the fix beyond
        this one customer, e.g. "Always include exhibits/appendices in
        document search scope for customer ID lookups."

    Returns:
      dict with "status": "logged" and "correction_id".
    """
    correction = Correction(
        correction_id=f"COR-{uuid.uuid4().hex[:6]}",
        field=field,
        customer=customer,
        wrong_answer=wrong_answer,
        correct_value=correct_value,
        correct_source=correct_source,
        root_cause=root_cause,
        proposed_rule=proposed_rule,
        reported_by=reported_by,
        created_at=datetime.now(UTC).isoformat(),
    )
    audit_log.append_event("correction", correction.model_dump())
    return {"status": "logged", "correction_id": correction.correction_id}
