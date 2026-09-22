"""Unit tests for the Document Search tool."""

from __future__ import annotations

from clause_agent.tools.document_search import search_documents


def test_finds_payment_term_in_body_by_default():
    result = search_documents(customer="Acme Corp", query="payment term")
    docs = {hit["doc"] for hit in result["hits"]}
    assert "2018_MSA_AcmeCorp.pdf" in docs
    assert "2025_Renewal_AcmeCorp_ProductX.pdf" in docs
    assert result["scope_searched"] == ["amendment", "body", "renewal"]


def test_finds_supersession_language():
    result = search_documents(customer="Acme Corp", query="payment term")
    texts = " ".join(hit["text"] for hit in result["hits"])
    assert "supersedes" in texts.lower()


def test_default_scope_excludes_exhibits():
    """Mirrors PRD TC3: default search must NOT see the exhibit."""
    result = search_documents(customer="Acme Corp", query="customer ID")
    assert result["hits"] == []


def test_widened_scope_finds_exhibit():
    result = search_documents(
        customer="Acme Corp",
        query="customer ID",
        scope=["body", "exhibit"],
    )
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["doc"] == "2018_MSA_AcmeCorp.pdf"
    assert hit["section"] == "Exhibit A"
    assert "100234" in hit["text"]


def test_unrelated_customer_not_matched():
    result = search_documents(customer="Acme Corp", query="payment term")
    docs = {hit["doc"] for hit in result["hits"]}
    assert "2022_MSA_Globex.pdf" not in docs


def test_unknown_scope_value_returns_error():
    result = search_documents(
        customer="Acme Corp", query="payment term", scope=["not_a_scope"]
    )
    assert result["hits"] == []
    assert "error" in result


def test_globex_customer_id_only_in_exhibit():
    default_result = search_documents(customer="Globex", query="customer ID")
    assert default_result["hits"] == []

    widened_result = search_documents(
        customer="Globex", query="customer ID", scope=["body", "exhibit"]
    )
    assert len(widened_result["hits"]) == 1
    assert widened_result["hits"][0]["section"] == "Exhibit B"
    assert "220987" in widened_result["hits"][0]["text"]
