"""Document Search tool: RAG-style retrieval over the contract corpus.

POC scope (PRD §5): a real document-ingestion pipeline is explicitly out of
scope. This module loads a small, structured JSON corpus (see
`clause_agent/data/contracts/corpus.json`) and does keyword/tag matching
instead of embeddings -- enough to prove the hierarchy-resolution and
citation logic without building a production RAG pipeline.

Design note (mirrors the PRD "Simulated Session" trace closely): each
section carries its own `doc_type` ("body" | "amendment" | "renewal" |
"exhibit" | "appendix"). `search_documents` defaults to the main written
pages only (`body`, `amendment`, `renewal`) -- callers must explicitly widen
`scope` to include `exhibit`/`appendix` to search attachments. This is not
an accident: TC3 in the PRD depends on the default search missing an
exhibit-only fact, so a downstream correction can teach the agent (via
Memory Bank) to widen its scope for that field going forward.
"""

from __future__ import annotations

import functools
import json
import re
from typing import Any

from clause_agent.shared_libraries import config
from clause_agent.shared_libraries.schemas import ClauseHit

DEFAULT_SCOPE = ("body", "amendment", "renewal")
VALID_DOC_TYPES = {"body", "amendment", "renewal", "exhibit", "appendix"}
MAX_SEARCH_HITS = 5

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


@functools.lru_cache(maxsize=1)
def _load_corpus() -> list[dict[str, Any]]:
    corpus_path = config.get_contracts_dir() / "corpus.json"
    with corpus_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data["documents"]


def _clear_corpus_cache() -> None:
    """Test-only hook to force a corpus reload after monkeypatching paths."""
    _load_corpus.cache_clear()


MIN_HIT_SCORE = 2
"""Minimum token-overlap score to count as a hit.

A score of 1 is too permissive: generic contract boilerplate words (e.g.
"Customer" appearing in an unrelated payment-terms sentence) would
otherwise false-positive-match a "customer ID" query against a section
that has nothing to do with customer IDs. Requiring >= 2 overlapping
tokens means a real match needs either a tag hit (tags are curated to be
specific, e.g. "customer_id" contributes 2 tokens) or genuine multi-word
overlap with the query.
"""


def _score(query_tokens: set[str], section: dict[str, Any]) -> int:
    haystack = section["text"] + " " + " ".join(section.get("tags", []))
    haystack_tokens = _tokenize(haystack.replace("_", " "))
    return len(query_tokens & haystack_tokens)


def search_documents(
    customer: str, query: str, scope: list[str] | None = None
) -> dict[str, Any]:
    """Searches the contract corpus for passages relevant to a query.

    Args:
      customer: Customer/company name to scope the search to, e.g.
        "Acme Corp". Matching is case-insensitive substring match.
      query: Free-text description of what is being looked for, e.g.
        "payment term" or "customer ID".
      scope: Which section types to search. Valid values: "body",
        "amendment", "renewal", "exhibit", "appendix". Defaults to
        ["body", "amendment", "renewal"] -- the main written pages only.
        You MUST explicitly pass a scope that includes "exhibit" and/or
        "appendix" to search attachments; check Memory Bank first for any
        standing rule about widening scope for the field you're looking up.

    Returns:
      A dict with:
        - "hits": list of {doc, section, doc_type, text, effective_date},
          sorted by relevance, most relevant first. Empty if nothing in the
          searched scope matched -- this is a real "not found", not an
          error, and the caller must not fabricate an answer in that case.
        - "scope_searched": the effective scope that was used.
    """
    effective_scope = set(scope) if scope else set(DEFAULT_SCOPE)
    unknown = effective_scope - VALID_DOC_TYPES
    if unknown:
        return {
            "error": (
                f"Unknown scope value(s): {sorted(unknown)}. Valid values:"
                f" {sorted(VALID_DOC_TYPES)}."
            ),
            "hits": [],
            "scope_searched": sorted(effective_scope),
        }

    query_tokens = _tokenize(query)
    customer_lower = customer.strip().lower()

    scored_hits: list[tuple[int, dict[str, Any]]] = []
    for doc in _load_corpus():
        if customer_lower not in doc["customer"].lower():
            continue
        for section in doc["sections"]:
            if section["doc_type"] not in effective_scope:
                continue
            score = _score(query_tokens, section)
            if score < MIN_HIT_SCORE:
                continue
            hit = ClauseHit(
                doc=doc["doc"],
                section=section["section"],
                doc_type=section["doc_type"],
                text=section["text"],
                effective_date=doc["effective_date"],
            )
            scored_hits.append((score, hit.model_dump()))

    scored_hits.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "hits": [hit for _, hit in scored_hits[:MAX_SEARCH_HITS]],
        "scope_searched": sorted(effective_scope),
    }
