# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Search the catalogue's products by meaning and by keywords.

The query is embedded with the catalogue's embedding model (the first
``EMBED_DIMS`` values, as the stored embeddings) and compared with every
product by cosine similarity; words of the query found in a product's
title, category, colour or audience (or, worth half, its description,
style or season) add a keyword boost. Products without an embedding are
ranked by their keyword match alone, and only when they have one. When the
embedding call fails, the search is keyword-only.
"""

import functools
import logging
import re
from collections.abc import Sequence
from typing import Any

import numpy as np

from genmedia4commerce.core import gen
from genmedia4commerce.core.catalog import (
    Catalog,
    audiences,
    featured,
    get_catalog,
)
from genmedia4commerce.core.errors import GenMediaError, InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import get_settings

__all__ = ["EMBED_DIMS", "MAX_K", "find", "search"]

logger = logging.getLogger(__name__)

EMBED_DIMS = 256
MAX_K = 50
#: What a full keyword match adds to the cosine similarity.
KEYWORD_WEIGHT = 0.15
#: The similarity a product without an embedding gets for a full keyword
#: match (a typical good semantic match).
NO_EMBEDDING_SIMILARITY = 0.65
_STRONG = ("title", "category", "color", "color_family", "audience")
_WEAK = ("description", "style", "season")
_STOPWORDS = frozenset(
    "a an and any for find from in is it me of on one or show some the "
    "to with".split()
)


def _stem(word: str) -> str:
    """A crude singular, the same for the query and the catalogue."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 1 and w not in _STOPWORDS
    }


@functools.lru_cache(maxsize=1)
def _index(cat: Catalog) -> list[tuple[set[str], set[str]]]:
    """The (strong, weak) words of every item."""

    def words(row: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
        return _words(" ".join(str(row.get(k) or "") for k in keys))

    return [(words(r, _STRONG), words(r, _WEAK)) for r in cat.rows]


def _keyword_scores(cat: Catalog, query: str) -> np.ndarray:
    """Per item, the share of the query's words it has (weak ones half)."""
    terms = _words(query)
    if not terms:
        return np.zeros(len(cat.rows))
    return np.array(
        [
            sum(1.0 if t in s else 0.5 if t in w else 0.0 for t in terms)
            / len(terms)
            for s, w in _index(cat)
        ]
    )


def _embed(query: str) -> np.ndarray | None:
    """The query's unit embedding, or ``None`` when the call fails."""
    try:
        vector = gen.embed(
            [query],
            model=get_models().embed_catalog,
            location=get_settings().region,
            output_dims=EMBED_DIMS,
        )[0]
    except GenMediaError as exc:
        logger.warning(f"catalogue search is keyword-only: {exc}")
        return None
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else None


def _allowed(cat: Catalog, audience: set[str], family: str) -> np.ndarray:
    return np.array(
        [
            r["kind"] == "product"
            and (not audience or r.get("audience") in audience)
            and (not family or r.get("family") == family)
            for r in cat.rows
        ],
        dtype=bool,
    )


def search(
    query: str,
    *,
    audience: Sequence[str] | str = (),
    family: str = "",
    k: int = 8,
) -> list[dict[str, Any]]:
    """The ``k`` best products for ``query``, best first, each its
    catalogue row plus ``score`` (see the module doc).

    ``audience`` ("women", "men", a list or a comma-separated mix) keeps
    those audiences and unisex items; ``family`` keeps one family.
    """
    query = query.strip()
    if not query:
        raise InputError("query must not be empty")
    if not 1 <= k <= MAX_K:
        raise InputError(f"k must be between 1 and {MAX_K}")
    cat = get_catalog()
    keywords = _keyword_scores(cat, query)
    vector = _embed(query)
    if vector is None:
        scores, found = keywords, keywords > 0
    else:
        similar = np.where(
            cat.has_embedding,
            cat.embeddings @ vector,
            NO_EMBEDDING_SIMILARITY * keywords,
        )
        scores = similar + KEYWORD_WEIGHT * keywords
        found = cat.has_embedding | (keywords > 0)
    found &= _allowed(cat, audiences(audience), family)
    top = sorted(np.flatnonzero(found), key=lambda i: -scores[i])[:k]
    return [{**cat.rows[i], "score": round(float(scores[i]), 4)} for i in top]


def find(
    query: str,
    *,
    audience: Sequence[str] | str = (),
    family: str = "",
    k: int = 8,
) -> list[dict[str, Any]]:
    """``search``, except that an empty query lists the featured products
    (at most ``k``, with the same filters)."""
    if query.strip():
        return search(query, audience=audience, family=family, k=k)
    if not 1 <= k <= MAX_K:
        raise InputError(f"k must be between 1 and {MAX_K}")
    return featured(audience, family)[:k]
