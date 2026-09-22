"""Memory Bank tool: scoped, structured long-term memory for ClauseIQ.

The PRD's "Simulated Session" trace calls `memory_bank_search`/
`memory_bank_create` with hierarchical scope strings, e.g.
`"customer:AcmeCorp/product:ProductX/clause:payment_term"` or
`"global/rule:document_search_scope"`. We represent that scope as a plain
`dict[str, str]` (e.g. `{"customer": "AcmeCorp", "product": "ProductX",
"clause": "payment_term"}`) -- this maps directly onto Vertex AI Memory
Bank's structured `scope` parameter, so swapping `LocalJsonMemoryBankBackend`
for a real `VertexMemoryBankBackend` later requires no agent/tool changes,
only a different `MemoryBankBackend` implementation (see `set_backend`).

Guardrail (PRD §8): a scope that includes a "clause" key represents a
precedence/interpretation *ruling*, which must never be written without
Legal approval. `memory_bank_create` enforces this structurally (not just
via prompt instructions): it refuses the write unless both `approved_by` and
`approved_at` are provided.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from clause_agent.shared_libraries import config
from clause_agent.shared_libraries.schemas import MemoryRecord

_lock = threading.Lock()

# Scope key that marks a memory as a precedence/interpretation ruling,
# which requires Legal approval before it may be written.
RULING_SCOPE_KEY = "clause"


class MemoryBankBackend(Protocol):
    """Storage interface for scoped memories.

    Implement this against real Vertex AI Memory Bank for production; the
    agent/tool code above only depends on this protocol, never on the
    concrete backend.
    """

    def search(self, scope: dict[str, str]) -> list[MemoryRecord]: ...

    def create(
        self,
        scope: dict[str, str],
        fact: str,
        citation: str | None,
        approved_by: str | None,
        approved_at: str | None,
        source_correction_id: str | None,
    ) -> MemoryRecord: ...


class LocalJsonMemoryBankBackend:
    """File-backed stand-in for Vertex AI Memory Bank (POC default).

    Memories are stored as a flat JSON list and matched by *exact*
    scope-dict equality. This is a simplification: real Vertex AI Memory
    Bank supports semantic similarity search within a scope. Because
    `search`/`create` in this module only depend on the `MemoryBankBackend`
    protocol, upgrading to real semantic search later is a backend swap,
    not an agent rewrite.
    """

    def __init__(self, path: Path | None = None):
        self._path = path

    def _resolve_path(self) -> Path:
        return self._path or config.get_memory_bank_path()

    def _read_all(self) -> list[dict[str, Any]]:
        path = self._resolve_path()
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_all(self, records: list[dict[str, Any]]) -> None:
        path = self._resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)

    def search(self, scope: dict[str, str]) -> list[MemoryRecord]:
        with _lock:
            records = self._read_all()
        return [
            MemoryRecord(**record)
            for record in records
            if record.get("scope") == scope
        ]

    def create(
        self,
        scope: dict[str, str],
        fact: str,
        citation: str | None = None,
        approved_by: str | None = None,
        approved_at: str | None = None,
        source_correction_id: str | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            memory_id=f"mem_{uuid.uuid4().hex[:8]}",
            scope=scope,
            fact=fact,
            citation=citation,
            approved_by=approved_by,
            approved_at=approved_at,
            created_at=datetime.now(UTC).isoformat(),
            source_correction_id=source_correction_id,
        )
        with _lock:
            records = self._read_all()
            records.append(json.loads(record.model_dump_json()))
            self._write_all(records)
        return record


_backend: MemoryBankBackend = LocalJsonMemoryBankBackend()


def get_backend() -> MemoryBankBackend:
    return _backend


def set_backend(backend: MemoryBankBackend) -> None:
    """Overrides the active backend. Used by tests and future Vertex wiring."""
    global _backend
    _backend = backend


def memory_bank_search(scope: dict[str, str]) -> dict[str, Any]:
    """Searches Memory Bank for previously confirmed facts/rules in a scope.

    Always call this before answering a question that could have a prior
    ruling, corrected fact, or standing rule -- e.g. before proposing a
    precedence ruling, or before extracting a field a correction may have
    already fixed.

    Args:
      scope: The exact scope to look up, e.g.
        {"customer": "Acme Corp", "product": "Product X",
         "clause": "payment_term"} for a customer/product-specific ruling,
        or {"rule_type": "document_search_scope", "field": "customer_id"}
        for a global, reusable rule.

    Returns:
      dict with "memories": list of matching records (empty if none found
      -- this is a normal "nothing on file yet", not an error).
    """
    hits = get_backend().search(scope)
    return {"memories": [json.loads(h.model_dump_json()) for h in hits]}


def memory_bank_create(
    scope: dict[str, str],
    fact: str,
    citation: str | None = None,
    approved_by: str | None = None,
    approved_at: str | None = None,
    source_correction_id: str | None = None,
) -> dict[str, Any]:
    """Writes a confirmed fact or reusable rule to Memory Bank.

    Args:
      scope: Scope dict this memory applies to (see `memory_bank_search`).
      fact: The confirmed fact or rule, in plain language, e.g.
        "60 days to pay" or "Always search exhibits/appendices for customer
        ID lookups.".
      citation: Source citation for the fact, e.g. "2025 Renewal §4.2".
      approved_by: Required when `scope` contains a "clause" key (a
        precedence/interpretation ruling) -- the Legal reviewer's identity
        from an *approved* `request_legal_review` task. Omit for
        field-value corrections or process rules, which come directly from
        the person who verified them.
      approved_at: Timestamp the Legal approval was granted (required
        alongside `approved_by`).
      source_correction_id: The `correction_id` from `submit_correction`,
        if this memory originated from a correction.

    Returns:
      dict with "status": "written" and "memory_id", or "status": "rejected"
      and "error" if a Legal-approval-required write was attempted without
      approval.
    """
    if RULING_SCOPE_KEY in scope and (not approved_by or not approved_at):
        return {
            "status": "rejected",
            "error": (
                "Refusing to write a clause-scoped ruling without Legal"
                " approval: both 'approved_by' and 'approved_at' are required."
                " Call request_legal_review and wait for approval first."
            ),
        }
    record = get_backend().create(
        scope=scope,
        fact=fact,
        citation=citation,
        approved_by=approved_by,
        approved_at=approved_at,
        source_correction_id=source_correction_id,
    )
    return {"status": "written", "memory_id": record.memory_id}
