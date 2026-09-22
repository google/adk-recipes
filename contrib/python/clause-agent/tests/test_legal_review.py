"""Unit tests for the Legal Review tool (request + local queue resolution)."""

from __future__ import annotations

import pytest

from clause_agent.tools.legal_review import (
    get_task,
    list_pending,
    request_legal_review,
    resolve_legal_review,
)


def test_request_returns_pending_task():
    result = request_legal_review(
        customer="Acme Corp",
        question="Does the 2025 renewal override the 2018 term for Product X?",
        proposed_answer="Yes -- 60 days, per 2025 Renewal §4.2/§9",
        sources=[
            "2018_MSA_AcmeCorp.pdf#§7",
            "2025_Renewal_AcmeCorp_ProductX.pdf#§4.2",
        ],
        confidence=0.78,
    )
    assert result["status"] == "pending"
    assert result["task_id"].startswith("LR-")

    task = get_task(result["task_id"])
    assert task is not None
    assert task.status == "pending"
    assert task.confidence == 0.78


def test_list_pending_only_returns_unresolved():
    r1 = request_legal_review(
        customer="Acme Corp",
        question="Q1",
        proposed_answer="A1",
        sources=[],
        confidence=0.5,
    )
    request_legal_review(
        customer="Globex Inc.",
        question="Q2",
        proposed_answer="A2",
        sources=[],
        confidence=0.6,
    )
    resolve_legal_review(
        task_id=r1["task_id"], decision="approved", approver="legal@x.com"
    )
    pending = list_pending()
    assert len(pending) == 1
    assert pending[0].customer == "Globex Inc."


def test_resolve_approved_records_approver_and_final_answer():
    result = request_legal_review(
        customer="Acme Corp",
        question="Q",
        proposed_answer="Net 60",
        sources=["src"],
        confidence=0.78,
    )
    resolved = resolve_legal_review(
        task_id=result["task_id"],
        decision="approved",
        approver="l.martinez@legal.acme",
        comment="Confirmed -- unambiguous override.",
    )
    assert resolved.status == "approved"
    assert resolved.approver == "l.martinez@legal.acme"
    assert resolved.final_answer == "Net 60"
    assert resolved.resolved_at is not None


def test_resolve_edited_overrides_final_answer():
    result = request_legal_review(
        customer="Acme Corp",
        question="Q",
        proposed_answer="Net 60",
        sources=["src"],
        confidence=0.6,
    )
    resolved = resolve_legal_review(
        task_id=result["task_id"],
        decision="edited",
        approver="legal@x.com",
        final_answer="Net 45, not Net 60 -- see amended schedule.",
    )
    assert resolved.status == "edited"
    assert (
        resolved.final_answer == "Net 45, not Net 60 -- see amended schedule."
    )


def test_resolve_unknown_task_raises():
    with pytest.raises(ValueError):
        resolve_legal_review(
            task_id="LR-doesnotexist", decision="approved", approver="x"
        )


def test_resolve_invalid_decision_raises():
    result = request_legal_review(
        customer="Acme Corp",
        question="Q",
        proposed_answer="A",
        sources=[],
        confidence=0.5,
    )
    with pytest.raises(ValueError):
        resolve_legal_review(
            task_id=result["task_id"], decision="maybe", approver="x"
        )


def test_resolution_is_audit_logged():
    from clause_agent.shared_libraries import audit_log

    result = request_legal_review(
        customer="Acme Corp",
        question="Q",
        proposed_answer="A",
        sources=[],
        confidence=0.5,
    )
    resolve_legal_review(
        task_id=result["task_id"], decision="approved", approver="legal@x.com"
    )
    events = audit_log.read_events()
    assert any(e["event_type"] == "legal_review_resolved" for e in events)
