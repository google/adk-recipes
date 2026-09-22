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
"""Ranking and the profile: the two places judgement is allowed to live.

Both are deterministic code rather than model output, and both are here for
the same reason. A model asked to justify a recommendation writes a fluent
sentence whether or not the match exists; a model asked to fill a profile
field offers "about two hundred" for a price ceiling. Neither failure is
visible at runtime, so both get pinned down here.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.profile import (
    DEFAULT_PROFILE,
    EDITABLE_FIELDS,
    PROFILE_KEY,
    PreferenceError,
    load_profile,
    update_preference,
)
from app.ranking import rank_products, score_product
from conftest import StubContext

PROFILE: dict[str, Any] = {
    "shoe_size": "9.5",
    "apparel_size": "M",
    "price_ceiling": 200.0,
    "favorite_brands": ["Aera"],
    "preferred_categories": ["trail-shoes"],
    "colors": ["moss", "slate"],
    "avoid_materials": ["responsible down"],
    "needs_waterproof": False,
}


def product(**overrides: Any) -> dict[str, Any]:
    """A catalog-shaped product with everything the ranking reads."""
    base: dict[str, Any] = {
        "id": "P-1",
        "name": "Test Runner",
        "brand": "Aera",
        "category": "trail-shoes",
        "price": 148.0,
        "rating": 4.5,
        "sizes": ["8", "9", "9.5", "10"],
        "colors": ["moss", "clay"],
        "attributes": {"Material": "recycled mesh", "Waterproof": False},
    }
    base.update(overrides)
    return base


# --- ranking ----------------------------------------------------------------


def test_every_chip_corresponds_to_a_real_match() -> None:
    """Chips are emitted by the rules that scored, so they cannot be invented."""
    scored = score_product(product(), PROFILE)
    assert scored.reasons == [
        "Your usual trail shoes",
        "In your size 9.5",
        "Aera, a brand you buy",
    ]


def test_chips_are_capped_so_a_card_stays_readable() -> None:
    """Five matches, three chips."""
    generous = {**PROFILE, "needs_waterproof": True}
    scored = score_product(
        product(attributes={"Material": "nylon", "Waterproof": True}),
        generous,
    )
    assert len(scored.reasons) == 3


def test_a_product_with_no_matches_earns_no_chips() -> None:
    """Nothing matched, so the card claims nothing.

    The score is still positive -- rating is a tiebreak -- but silence beats a
    manufactured reason.
    """
    scored = score_product(
        product(
            brand="Unknown Co",
            category="accessory",
            price=900.0,
            colors=["neon"],
            sizes=["one size"],
        ),
        PROFILE,
    )
    assert scored.reasons == []


def test_penalties_are_silent() -> None:
    """An over-budget item can still appear, but never brags about price."""
    scored = score_product(product(price=450.0), PROFILE)
    assert not any("Under your" in reason for reason in scored.reasons)
    assert scored.score < score_product(product(), PROFILE).score


def test_unavailable_size_is_excluded_not_ranked_low() -> None:
    """A shoe that is not stocked in the shopper's size is not a weak
    recommendation -- it is not a recommendation."""
    scored = score_product(product(sizes=["11", "12"]), PROFILE)
    assert scored.excluded == "size unavailable"
    assert rank_products([product(sizes=["11", "12"])], PROFILE) == []


def test_avoided_material_is_excluded() -> None:
    """Substring match, so "responsible down fill" is caught too."""
    scored = score_product(
        product(attributes={"Material": "responsible down fill"}), PROFILE
    )
    assert scored.excluded == "material excluded"


def test_one_size_products_are_available_but_not_chip_worthy() -> None:
    scored = score_product(product(sizes=["one size"]), PROFILE)
    assert scored.excluded is None
    assert not any("size" in reason for reason in scored.reasons)


def test_apparel_ranges_cover_the_sizes_they_span() -> None:
    """``S/M`` stocks an M."""
    scored = score_product(
        product(category="mid-layer", sizes=["XS/S", "S/M", "L/XL"]), PROFILE
    )
    assert scored.excluded is None
    assert "In your size S/M" in scored.reasons


def test_ranking_is_deterministic_including_ties() -> None:
    """Same input, same order -- ties broken on id, not dict order.

    The second list holds the same three products in a different order, so a
    ranking that leaned on input order would disagree with the first.
    """
    ranked = rank_products(
        [product(id="P-3"), product(id="P-1"), product(id="P-2")], PROFILE
    )
    reordered = rank_products(
        [product(id="P-2"), product(id="P-3"), product(id="P-1")], PROFILE
    )
    assert [s.product["id"] for s in ranked] == ["P-1", "P-2", "P-3"]
    assert [s.product["id"] for s in reordered] == ["P-1", "P-2", "P-3"]


def test_the_limit_is_honoured() -> None:
    """A limit other than the default, so an ignored argument would show."""
    many = [product(id=f"P-{i}") for i in range(10)]
    assert len(rank_products(many, PROFILE, limit=5)) == 5
    assert len(rank_products(many, PROFILE)) == 3


def test_preferences_change_the_order() -> None:
    """The property the whole demo turns on.

    Raise the ceiling above an expensive shell and it outranks a cheap one it
    previously lost to.
    """
    shell = product(id="P-SHELL", price=320.0, rating=4.9)
    cheap = product(id="P-CHEAP", price=90.0, rating=4.0)

    tight = rank_products([shell, cheap], {**PROFILE, "price_ceiling": 100.0})
    assert [s.product["id"] for s in tight] == ["P-CHEAP", "P-SHELL"]

    loose = rank_products([shell, cheap], {**PROFILE, "price_ceiling": 400.0})
    assert [s.product["id"] for s in loose] == ["P-SHELL", "P-CHEAP"]


# --- the profile ------------------------------------------------------------


def test_load_profile_fills_in_missing_fields(ctx: StubContext) -> None:
    """A profile stored by an older version still has every field.

    The ranking reads eight fields; a partial profile would make it silently
    skip whichever rules those fields drive.
    """
    ctx.state[PROFILE_KEY] = {"shoe_size": "11"}
    loaded = load_profile(ctx.state)
    assert loaded["shoe_size"] == "11"
    assert set(loaded) == set(DEFAULT_PROFILE)


def test_an_empty_state_yields_the_default_profile(ctx: StubContext) -> None:
    assert load_profile(ctx.state) == DEFAULT_PROFILE


def test_the_profile_is_written_to_a_user_scoped_key(
    ctx: StubContext,
) -> None:
    """``user:`` scope is what makes a preference outlive the session."""
    update_preference(ctx.state, "shoe_size", "10")
    assert PROFILE_KEY.startswith("user:")
    assert ctx.state[PROFILE_KEY]["shoe_size"] == "10"


@pytest.mark.parametrize(
    ("field", "value", "stored"),
    [
        ("price_ceiling", "$1,250.50", 1250.5),
        ("price_ceiling", 150, 150.0),
        ("needs_waterproof", "yes", True),
        ("needs_waterproof", "not required", False),
        (
            "favorite_brands",
            "Aera, Halden; Coteau",
            ["Aera", "Halden", "Coteau"],
        ),
        ("favorite_brands", ["Aera", "aera", "Halden"], ["Aera", "Halden"]),
        ("colors", "  moss ,, slate ", ["moss", "slate"]),
        ("shoe_size", " 10.5 ", "10.5"),
    ],
)
def test_values_a_model_would_offer_are_coerced(
    ctx: StubContext, field: str, value: Any, stored: Any
) -> None:
    """Currency symbols, prose booleans, and messy lists all arrive in practice."""
    _, coerced = update_preference(ctx.state, field, value)
    assert coerced == stored


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price_ceiling", "about two hundred"),
        ("price_ceiling", "-50"),
        ("price_ceiling", True),
        ("needs_waterproof", "sometimes"),
        ("favorite_brands", ""),
        ("favorite_brands", 42),
        ("shoe_size", "   "),
        ("display_name", "Someone Else"),
        ("purchase_history", "[]"),
    ],
)
def test_values_that_would_break_the_ranking_are_refused(
    ctx: StubContext, field: str, value: Any
) -> None:
    """Refusing beats storing nonsense and ranking on it from then on.

    ``display_name`` and ``purchase_history`` are refused for a different
    reason: they are not preferences. History is derived from orders, so a
    writable copy could only ever disagree with the source.
    """
    with pytest.raises(PreferenceError):
        update_preference(ctx.state, field, value)
    assert PROFILE_KEY not in ctx.state


def test_a_rejection_names_the_editable_fields(ctx: StubContext) -> None:
    """The agent needs to know what to offer next, not just that it failed."""
    with pytest.raises(PreferenceError) as caught:
        update_preference(ctx.state, "budget", "200")
    message = str(caught.value)
    assert "price_ceiling" in message
    assert all(field in message for field in EDITABLE_FIELDS)
