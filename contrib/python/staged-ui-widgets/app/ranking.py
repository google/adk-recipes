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
"""Deterministic ranking that produces its own explanations.

The reason chips on a product card are the most tempting thing in this recipe
to let the model write, and the worst. A model asked to justify a
recommendation will produce a fluent sentence whether or not the underlying
match exists -- "great for your wide feet" about a profile that says nothing
about feet. Here every chip is emitted by the rule that scored the point, so
a chip cannot appear unless the match it describes actually happened.

The same property makes the ranking testable: same profile plus same catalog
gives the same order, every run, on every machine. Ties break on product id
rather than dict order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import store
from .render.converters import money

# Weights. Positive signals earn a chip; penalties are silent, because a card
# reading "above your budget" is an odd thing to recommend.
_W_CATEGORY = 30
_W_SIZE = 22
_W_BRAND = 20
_W_WATERPROOF = 18
_W_UNDER_BUDGET = 15
_W_COLOR = 10

_P_OVER_BUDGET = -28
_P_ALREADY_OWNED = -45

# Hard exclusions -- reasons a product should not be shown at all, rather
# than shown with a low score.
_EXCLUDE_NO_SIZE = "size unavailable"
_EXCLUDE_MATERIAL = "material excluded"

# Chips beyond the third stop being read and start being clutter.
_MAX_REASONS = 3

# Categories sized by shoe size rather than apparel size.
_FOOTWEAR = {"trail-shoes", "road-shoes"}
_ANY_SIZE = {"one size"}


@dataclass
class Scored:
    """One product with its score and the reasons that produced it."""

    product: dict[str, Any]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    excluded: str | None = None


def rank_products(
    candidates: list[Mapping[str, Any]],
    profile: Mapping[str, Any],
    *,
    limit: int = 3,
) -> list[Scored]:
    """Scores candidates against the profile and returns the best few.

    Excluded products are dropped rather than ranked last: a shoe that does
    not come in the shopper's size is not a weak recommendation, it is not a
    recommendation.
    """
    scored = [score_product(c, profile) for c in candidates]
    keepable = [s for s in scored if s.excluded is None]
    keepable.sort(key=lambda s: (-s.score, s.product["id"]))
    return keepable[:limit]


def score_product(
    candidate: Mapping[str, Any], profile: Mapping[str, Any]
) -> Scored:
    """Scores one product, collecting a chip per positive match."""
    result = Scored(product=dict(candidate))
    attributes = candidate.get("attributes", {})
    attributes = attributes if isinstance(attributes, Mapping) else {}

    # --- hard exclusions ----------------------------------------------------
    material = str(attributes.get("Material", "")).lower()
    avoided = [str(a).lower() for a in profile.get("avoid_materials", [])]
    if material and any(a and a in material for a in avoided):
        result.excluded = _EXCLUDE_MATERIAL
        return result

    size_label = _matching_size(candidate, profile)
    if size_label is None:
        result.excluded = _EXCLUDE_NO_SIZE
        return result

    # --- positive signals ---------------------------------------------------
    if candidate.get("category") in profile.get("preferred_categories", []):
        result.score += _W_CATEGORY
        result.reasons.append(
            f"Your usual {_readable(str(candidate['category']))}"
        )

    if size_label not in _ANY_SIZE:
        result.score += _W_SIZE
        result.reasons.append(f"In your size {size_label}")

    if candidate.get("brand") in profile.get("favorite_brands", []):
        result.score += _W_BRAND
        result.reasons.append(f"{candidate['brand']}, a brand you buy")

    if profile.get("needs_waterproof") and attributes.get("Waterproof"):
        result.score += _W_WATERPROOF
        result.reasons.append("Waterproof")

    ceiling = profile.get("price_ceiling")
    price = candidate.get("price")
    if isinstance(ceiling, (int, float)) and isinstance(price, (int, float)):
        if price <= ceiling:
            result.score += _W_UNDER_BUDGET
            result.reasons.append(f"Under your {money(ceiling)}")
        else:
            result.score += _P_OVER_BUDGET

    shared_color = _shared_color(candidate, profile)
    if shared_color:
        result.score += _W_COLOR
        result.reasons.append(f"Comes in {shared_color}")

    if candidate.get("id") in store.purchased_product_ids():
        result.score += _P_ALREADY_OWNED

    # Rating is a tiebreak, not a signal worth a chip -- every product has
    # one, so a "well rated" chip on every card says nothing.
    rating = candidate.get("rating")
    if isinstance(rating, (int, float)):
        result.score += float(rating)

    result.reasons = result.reasons[:_MAX_REASONS]
    return result


def _matching_size(
    candidate: Mapping[str, Any], profile: Mapping[str, Any]
) -> str | None:
    """The shopper's size in this product, or ``None`` if it is not stocked.

    Returns the literal ``"one size"`` for products that do not vary, which
    the caller treats as available but not chip-worthy.
    """
    sizes = [str(s) for s in candidate.get("sizes", [])]
    if not sizes:
        return "one size"
    if any(s.lower() in _ANY_SIZE for s in sizes):
        return "one size"

    wanted = (
        profile.get("shoe_size")
        if candidate.get("category") in _FOOTWEAR or _looks_numeric(sizes)
        else profile.get("apparel_size")
    )
    if not wanted:
        return "one size"

    wanted = str(wanted).strip().lower()
    for size in sizes:
        if size.strip().lower() == wanted:
            return size
        # "S/M" and "L/XL" style ranges cover several apparel sizes.
        if "/" in size and wanted in [
            part.strip().lower() for part in size.split("/")
        ]:
            return size
    return None


def _looks_numeric(sizes: list[str]) -> bool:
    """Whether a size run is numeric, i.e. footwear-shaped."""
    return all(s.replace(".", "", 1).isdigit() for s in sizes)


def _shared_color(
    candidate: Mapping[str, Any], profile: Mapping[str, Any]
) -> str | None:
    """The first product colour the shopper likes, in the shopper's order."""
    stocked = {str(c).lower(): str(c) for c in candidate.get("colors", [])}
    for liked in profile.get("colors", []):
        match = stocked.get(str(liked).lower())
        if match:
            return match
    return None


def _readable(category: str) -> str:
    """``trail-shoes`` -> ``trail shoes``."""
    return category.replace("-", " ")
