"""Legal Review tool: blocking human-in-the-loop gate for precedence rulings.

Wrapped as a `LongRunningFunctionTool` in
`clause_agent/sub_agents/hierarchy_resolver.py`
(`google.adk.tools.long_running_tool.LongRunningFunctionTool`). Per ADK's
HITL pattern for long-running tools:

  1. `request_legal_review` runs synchronously and returns immediately with
     a "pending" status -- the model's turn ends there and it tells the
     user it's waiting.
  2. Later, a human resolves the task out-of-band (here: via
     `resolve_legal_review`, called by `scripts/legal_review_cli.py` or a
     test harness). That does NOT call back into the agent by itself.
  3. The host application or runner (see `tests/test_end_to_end_trace.py`)
     is responsible for noticing the pending long-running call, and -- once
     `resolve_legal_review` has recorded a decision -- sending a new
     `types.Content` containing a `function_response` for that same call
     id back through `Runner.run_async(...)` to resume the paused turn.

This module only implements steps 1 and 2 (the tool + the local queue); step
3 is host/runtime plumbing, kept separate so it's testable without a real
LLM.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clause_agent.shared_libraries import audit_log, config
from clause_agent.shared_libraries.schemas import LegalReviewTask

_lock = threading.Lock()


def _resolve_path(path: Path | None) -> Path:
    return path or config.get_legal_queue_path()


def _read_all(path: Path | None = None) -> list[dict[str, Any]]:
    target = _resolve_path(path)
    if not target.exists():
        return []
    with target.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(records: list[dict[str, Any]], path: Path | None = None) -> None:
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)


def request_legal_review(
    customer: str,
    question: str,
    proposed_answer: str,
    sources: list[str],
    confidence: float,
) -> dict[str, Any]:
    """Requests a blocking Legal review for a precedence/interpretation call.

    Use this whenever a ruling is precedent-setting for this customer/scope
    (no prior approved Memory Bank ruling exists) or your confidence is
    below the required bar. Do NOT answer the user directly in that case --
    this call pauses until a real Legal Reviewer approves, edits, or
    rejects the proposed ruling.

    Args:
      customer: Customer/company the ruling applies to.
      question: The precedence/interpretation question being asked.
      proposed_answer: Your proposed ruling and its rationale.
      sources: List of "doc#section" citations backing the proposal.
      confidence: Your confidence in the proposed answer, 0.0-1.0.

    Returns:
      dict with "status": "pending" and "task_id" -- the task is not yet
      resolved. Tell the user you're waiting on Legal; do not treat
      `proposed_answer` as fact until a later turn confirms approval.
    """
    task = LegalReviewTask(
        task_id=f"LR-{uuid.uuid4().hex[:5]}",
        customer=customer,
        question=question,
        proposed_answer=proposed_answer,
        sources=sources,
        confidence=confidence,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )
    with _lock:
        records = _read_all()
        records.append(json.loads(task.model_dump_json()))
        _write_all(records)
    return {"status": "pending", "task_id": task.task_id}


def get_task(task_id: str, path: Path | None = None) -> LegalReviewTask | None:
    """Looks up a Legal-review task by id. Used by CLI/tests, not the agent."""
    with _lock:
        for record in _read_all(path):
            if record.get("task_id") == task_id:
                return LegalReviewTask(**record)
    return None


def list_pending(path: Path | None = None) -> list[LegalReviewTask]:
    """Lists all pending Legal-review tasks. Used by the mock reviewer CLI."""
    with _lock:
        return [
            LegalReviewTask(**r)
            for r in _read_all(path)
            if r.get("status") == "pending"
        ]


def resolve_legal_review(
    task_id: str,
    decision: str,
    approver: str,
    comment: str | None = None,
    final_answer: str | None = None,
    path: Path | None = None,
) -> LegalReviewTask:
    """Records a human Legal Reviewer's decision on a pending task.

    This is called by the *human-facing* surface (CLI script or test
    harness) -- never by the agent itself. It only updates the local queue
    and the audit log; resuming the paused agent turn is a separate step
    (see `tests/test_end_to_end_trace.py`).

    Args:
      task_id: The task to resolve, e.g. "LR-a1b2c".
      decision: One of "approved", "edited", "rejected".
      approver: Identity of the Legal Reviewer making the decision.
      comment: Optional free-text comment from the reviewer.
      final_answer: The final ruling text. Required for "edited"; defaults
        to the task's `proposed_answer` for "approved".
      path: Override the legal queue location (used by tests).

    Returns:
      The updated `LegalReviewTask`.

    Raises:
      ValueError: If `task_id` does not exist, `decision` is invalid, or
        `decision` is "edited" without providing `final_answer`.
    """
    if decision not in {"approved", "edited", "rejected"}:
        raise ValueError(f"Invalid decision: {decision!r}")
    if decision == "edited" and not final_answer:
        raise ValueError("final_answer is required when decision is 'edited'")

    with _lock:
        records = _read_all(path)
        for record in records:
            if record.get("task_id") == task_id:
                record["status"] = decision
                record["approver"] = approver
                record["comment"] = comment
                record["final_answer"] = final_answer or record.get(
                    "proposed_answer"
                )
                record["resolved_at"] = datetime.now(UTC).isoformat()
                _write_all(records, path)
                resolved = LegalReviewTask(**record)
                break
        else:
            raise ValueError(f"Unknown Legal-review task_id: {task_id!r}")

    audit_log.append_event(
        "legal_review_resolved",
        {
            "task_id": resolved.task_id,
            "customer": resolved.customer,
            "decision": decision,
            "approver": approver,
            "comment": comment,
            "final_answer": resolved.final_answer,
        },
    )
    return resolved
