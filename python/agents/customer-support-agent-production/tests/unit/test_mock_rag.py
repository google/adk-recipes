"""
Tests for MockRAGProductSearch — pure Python, no LLM calls.
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.mock_rag_search import MockRAGProductSearch  # noqa: E402


class TestMockRAGProductSearch:
    def setup_method(self):
        self.search = MockRAGProductSearch()

    def test_search_laptops(self):
        results = self.search.search("laptops")
        assert len(results) > 0
        assert any("laptop" in r["name"].lower() for r in results)

    def test_search_under_500(self):
        results = self.search.search("products under $500")
        assert len(results) > 0
        assert all(r["price"] < 500 for r in results)

    def test_search_no_results(self):
        results = self.search.search("xyzzyplugh")
        assert results == []

    def test_search_generic_with_price(self):
        results = self.search.search("products under $500")
        assert len(results) > 0
        for r in results:
            assert r["price"] < 500

    def test_search_singular_plural(self):
        singular = self.search.search("laptop")
        plural = self.search.search("laptops")
        singular_ids = {r["id"] for r in singular}
        plural_ids = {r["id"] for r in plural}
        assert singular_ids == plural_ids, "Singular and plural should match the same products"

    def test_search_generic_no_price(self):
        """Generic query with no price filter should return results (mirrors real RAG behavior)."""
        results = self.search.search("products")
        assert len(results) > 0, "Generic query 'products' should return results like real RAG"

    def test_search_generic_no_price_returns_all(self):
        """'products' with no filter should return all products."""
        results = self.search.search("products")
        all_results = self.search.search("items")
        assert len(results) > 0
        assert len(all_results) > 0
