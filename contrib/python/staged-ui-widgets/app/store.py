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
"""Read access to the fictional catalog, orders, and spend history.

Everything is a JSON file under ``data/``, loaded once and cached. There is
no database and no network, which is deliberate: the recipe is about the
rendering pipeline, and a reviewer should be able to clone it and see real
widgets without provisioning anything.

``today()`` comes from the fixture rather than the clock. A demo whose
"arriving tomorrow" turns into "arriving three months ago" reads as broken,
and a test that depends on the real date is a test that fails on a Tuesday.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=8)
def _load(filename: str) -> dict[str, Any]:
    with (_DATA_DIR / filename).open(encoding="utf-8") as handle:
        return json.load(handle)


def _copies(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Independent copies of cached records.

    Every accessor that returns records goes through this or ``deepcopy``
    directly. ``_load`` caches for the process, so handing out the loaded dicts
    would let one caller's in-place edit follow a product through every later
    read -- a fixture layer that behaves like a database until it does not.
    Deep rather than ``dict(record)``: the nested values (``sizes``,
    ``stages``) are the ones a caller is most likely to sort or append to.
    """
    return [deepcopy(dict(record)) for record in records]


def products() -> list[dict[str, Any]]:
    """Every product in the fictional catalog."""
    return _copies(_load("products.json")["products"])


@lru_cache(maxsize=1)
def _products_by_id() -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in _load("products.json")["products"]}


def product(product_id: str) -> dict[str, Any] | None:
    """One product, or ``None`` when the id is unknown."""
    found = _products_by_id().get(product_id)
    return deepcopy(found) if found else None


def find_products(query: str) -> list[dict[str, Any]]:
    """Every-token match over name, brand, category, and tags.

    Deliberately simple. Retrieval quality is another recipe's subject; this
    one only needs enough matching to reach a widget.

    Token-wise rather than one substring, because the categories are slugs:
    a plain ``"trail shoes" in haystack`` never matches ``trail-shoes``, and
    the flagship query for the flagship category would silently fall through
    to the profile.
    """
    tokens = _tokens(query)
    if not tokens:
        return []
    matches = []
    for item in products():
        haystack = _tokens(
            " ".join(
                [
                    item["name"],
                    item["brand"],
                    item["category"],
                    " ".join(item.get("tags", [])),
                ]
            )
        )
        if all(any(token in word for word in haystack) for token in tokens):
            matches.append(item)
    return matches


def _tokens(text: str) -> list[str]:
    """Lowercased words, with slugs and punctuation split apart."""
    return [word for word in re.split(r"[^a-z0-9.]+", text.lower()) if word]


def orders() -> list[dict[str, Any]]:
    """Order history, newest first."""
    return _copies(
        sorted(
            _load("orders.json")["orders"],
            key=lambda o: o["placed_on"],
            reverse=True,
        )
    )


def order(order_id: str) -> dict[str, Any] | None:
    """One order by id, case-insensitively."""
    wanted = order_id.strip().upper()
    for candidate in orders():
        if candidate["id"].upper() == wanted:
            # Already an independent copy; ``orders()`` handed it over.
            return candidate
    return None


def latest_open_order() -> dict[str, Any] | None:
    """The newest order that has not finished.

    "Where is my order" almost always means the one still moving, so resolve
    it here rather than making the model guess an id.
    """
    for candidate in orders():
        stages = candidate.get("stages", [])
        if any(not stage.get("reached") for stage in stages):
            return candidate
    return None


def purchased_product_ids() -> set[str]:
    """Products the shopper already owns.

    Cancelled orders do not count -- the parcel never arrived, so the item is
    still a reasonable thing to recommend.
    """
    owned: set[str] = set()
    for candidate in orders():
        states = {
            str(stage.get("state", "")).lower()
            for stage in candidate.get("stages", [])
        }
        if "cancelled" in states:
            continue
        owned.update(candidate.get("item_ids", []))
    return owned


def today() -> str:
    """The fixture's notion of today, as ``YYYY-MM-DD``."""
    return str(_load("orders.json")["today"])


def spend_months() -> list[dict[str, Any]]:
    """Monthly spend, oldest first."""
    return _copies(_load("spend.json")["months"])


def currency() -> str:
    """The currency code the spend history is denominated in."""
    return str(_load("spend.json").get("currency", "USD"))
