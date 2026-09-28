"""Shared Pydantic schemas used across ClauseIQ tools and agent outputs.

Kept as plain, JSON-serializable models (see adk-style Pydantic patterns) so
they can flow through tool return values, Memory Bank records, and the audit
log without ad-hoc dict shapes drifting out of sync.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClauseHit(BaseModel):
    """A single passage retrieved from the contract corpus."""

    doc: str = Field(
        description="Source document id, e.g. '2018_MSA_AcmeCorp.pdf'."
    )
    section: str = Field(
        description="Section/clause id within the document, e.g. '§7'."
    )
    doc_type: str = Field(
        description=(
            "One of: body, amendment, renewal, exhibit, appendix. Exhibits"
            " and appendices are excluded from search unless explicitly"
            " requested via the `scope` argument."
        )
    )
    text: str = Field(description="The exact passage text.")
    effective_date: str = Field(
        description="ISO date (YYYY-MM-DD) the document/section took effect."
    )


class RulingProposal(BaseModel):
    """A hierarchy/precedence ruling proposed by hierarchy_resolver."""

    question: str
    proposed_answer: str
    citations: list[str] = Field(
        description="'doc#section' strings supporting the proposed answer."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class MemoryRecord(BaseModel):
    """A single fact/rule persisted to (or retrieved from) Memory Bank."""

    memory_id: str
    scope: dict[str, str]
    fact: str
    citation: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    created_at: str
    source_correction_id: str | None = None


class Correction(BaseModel):
    """A structured correction submitted by a downstream (Billing) user."""

    correction_id: str
    field: str
    customer: str
    wrong_answer: str
    correct_value: str
    correct_source: str
    root_cause: str
    proposed_rule: str | None = None
    reported_by: str
    created_at: str


class LegalReviewTask(BaseModel):
    """A Legal-review task tracked in the local legal queue."""

    task_id: str
    customer: str
    question: str
    proposed_answer: str
    sources: list[str]
    confidence: float
    status: str = "pending"  # pending | approved | edited | rejected
    approver: str | None = None
    comment: str | None = None
    final_answer: str | None = None
    created_at: str
    resolved_at: str | None = None
