"""Unit tests for the Memory Bank tool and its local backend."""

from __future__ import annotations

from clause_agent.tools.memory_bank import (
    memory_bank_create,
    memory_bank_search,
)


def test_search_empty_when_nothing_written():
    result = memory_bank_search(scope={"customer": "Acme Corp"})
    assert result == {"memories": []}


def test_ruling_write_rejected_without_approval():
    result = memory_bank_create(
        scope={"customer": "Acme Corp", "clause": "payment_term"},
        fact="60 days to pay",
    )
    assert result["status"] == "rejected"
    assert "approved_by" in result["error"]

    # And nothing was actually persisted.
    search_result = memory_bank_search(
        scope={"customer": "Acme Corp", "clause": "payment_term"}
    )
    assert search_result == {"memories": []}


def test_ruling_write_rejected_without_approved_at():
    result = memory_bank_create(
        scope={"customer": "Acme Corp", "clause": "payment_term"},
        fact="60 days to pay",
        approved_by="l.martinez@legal.acme",
        # missing approved_at
    )
    assert result["status"] == "rejected"
    assert "approved_at" in result["error"]

    search_result = memory_bank_search(
        scope={"customer": "Acme Corp", "clause": "payment_term"}
    )
    assert search_result == {"memories": []}


def test_ruling_write_allowed_with_approval():
    result = memory_bank_create(
        scope={"customer": "Acme Corp", "clause": "payment_term"},
        fact="60 days to pay, per 2025 Renewal",
        citation="2025 Renewal §4.2",
        approved_by="l.martinez@legal.acme",
        approved_at="2026-08-04T14:32:00Z",
    )
    assert result["status"] == "written"
    assert result["memory_id"].startswith("mem_")

    search_result = memory_bank_search(
        scope={"customer": "Acme Corp", "clause": "payment_term"}
    )
    assert len(search_result["memories"]) == 1
    memory = search_result["memories"][0]
    assert memory["fact"] == "60 days to pay, per 2025 Renewal"
    assert memory["approved_by"] == "l.martinez@legal.acme"


def test_field_correction_write_does_not_require_approval():
    result = memory_bank_create(
        scope={"customer": "Acme Corp", "field": "customer_id"},
        fact="100234",
        citation="2018 contract, Exhibit A",
    )
    assert result["status"] == "written"


def test_scope_is_exact_match_not_fuzzy():
    memory_bank_create(
        scope={"customer": "Acme Corp", "field": "customer_id"},
        fact="100234",
    )
    # A narrower/different scope must not match.
    result = memory_bank_search(scope={"customer": "Acme Corp"})
    assert result == {"memories": []}


def test_global_rule_scope_is_independent_of_customer_scope():
    memory_bank_create(
        scope={"rule_type": "document_search_scope", "field": "customer_id"},
        fact="Always search exhibits/appendices for customer ID lookups.",
        source_correction_id="COR-4471",
    )
    global_result = memory_bank_search(
        scope={"rule_type": "document_search_scope", "field": "customer_id"}
    )
    assert len(global_result["memories"]) == 1

    customer_scoped_result = memory_bank_search(
        scope={"customer": "Globex Inc.", "field": "customer_id"}
    )
    assert customer_scoped_result == {"memories": []}
