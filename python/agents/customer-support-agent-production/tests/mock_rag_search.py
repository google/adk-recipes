"""
Mock RAG Product Search for Deterministic Tests
==================================================
Keyword-based search over seed data — no embeddings needed.
"""

from typing import Dict, List, Optional

from customer_support_mas.database.fixtures import get_sample_data


class MockRAGProductSearch:
    """Keyword-based product search using seed data."""

    def __init__(self, *args, **kwargs):
        # Accept any init args (database_id, location) but ignore them
        self._products = get_sample_data()["products"]

    def search(self, query: str, limit: int = 5, max_price: Optional[float] = None) -> List[Dict]:
        import re

        if max_price is None:
            for pattern in [r"under\s*\$?(\d+)", r"below\s*\$?(\d+)", r"less than\s*\$?(\d+)"]:
                m = re.search(pattern, query.lower())
                if m:
                    max_price = float(m.group(1))
                    break

        query_lower = query.lower()
        # Remove price phrases from tokens so they don't pollute matching
        clean_query = re.sub(r"(under|below|less than)\s*\$?\d+", "", query_lower).strip()
        tokens = clean_query.split()

        # Generic tokens that should match all products (e.g. "products", "products under $500")
        # Real RAG returns results for generic queries — mock should too.
        generic_words = {"product", "products", "item", "items", "all", "everything", "anything", "show", "me"}
        meaningful_tokens = [t for t in tokens if t not in generic_words]
        match_all = len(meaningful_tokens) == 0

        results = []
        for pid, p in self._products.items():
            searchable = " ".join(
                [
                    p.get("name", ""),
                    p.get("description", ""),
                    p.get("category", ""),
                    " ".join(p.get("keywords", [])),
                ]
            ).lower()

            def token_matches(t, s):
                """Check token with singular/plural fallback."""
                if t in s:
                    return True
                if t.endswith("s") and t[:-1] in s:
                    return True
                if not t.endswith("s") and (t + "s") in s:
                    return True
                return False

            if match_all:
                score = 1
            else:
                score = sum(1 for t in tokens if token_matches(t, searchable))
            if score > 0:
                entry = {
                    "id": pid,
                    "name": p["name"],
                    "price": p["price"],
                    "category": p["category"],
                    "description": p["description"],
                    "similarity": 1.0 if match_all else score / len(tokens),
                }
                if max_price and entry["price"] > max_price:
                    continue
                results.append(entry)

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]
