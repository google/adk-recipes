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
"""Recommendation and comparison tools.

Both follow the same shape, which is the shape every tool in this recipe
follows: compute the facts, stage a payload, return a short summary. Neither
builds a component, names a colour, or decides a layout -- and neither calls
``render_ui_widget``.

The return value is written for the model; the widget is written for the
person. So the return carries facts the model may need to reason about --
ids, prices, why each item matched -- and leaves out everything that is the
card's business, like the image and the layout. Keeping the reply from
duplicating the card is the instruction's job, not the tool's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.adk.tools import ToolContext

from .. import store
from ..profile import load_profile
from ..ranking import rank_products
from ..render.placeholder_svg import product_tile_uri
from ..staging import clear_staged, spec_for, stage_widget, suppress_widget
from ..staging.state import is_suppressed, register_payload, was_emitted

# How many cards fit a carousel before it stops being scannable.
_MAX_PICKS = 3

# Attribute column order in a comparison. Anything not listed keeps the order
# it is first seen in, so the table is stable across runs.
_PREFERRED_COLUMNS = ("Price", "Weight", "Waterproof", "Material", "Warranty")

# Columns holding currency amounts. A fact about the data, not a format: the
# converter decides whether that becomes "$148" or "148,00 €".
_MONEY_COLUMNS = ("Price",)


def get_personalized_picks(
    query: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Recommends products for the shopper and shows them as cards.

    Ranking uses the stored shopper profile: preferred categories, favourite
    brands, size availability, price ceiling, and colours. The shopper sees
    the results as a card carousel, so do not list the items again in your
    reply -- refer to them.

    Args:
      query: What to look for, e.g. "trail shoes" or "something warm". Pass
        an empty string to recommend from the shopper's preferred categories.

    Returns:
      A summary of what was staged, including each pick's id, name, price,
      and the reasons it matched. The reasons are the chips printed on the
      card: quote them if the shopper asks why an item was recommended, and
      do not offer a different rationale.
    """
    profile = load_profile(tool_context.state)
    candidates, from_query = _candidates(query, profile)

    if not candidates:
        # Only reachable on the fallback path -- a query that matched would
        # have produced candidates -- so it is the profile that came up empty,
        # and blaming the query would misdescribe it.
        return {
            "status": "empty",
            "widget": None,
            "summary": (
                "Nothing in the shopper's preferred categories to recommend."
            ),
            "items": [],
        }

    picks = rank_products(candidates, profile, limit=_MAX_PICKS)
    if not picks:
        # Name the set that was actually scored. On the fallback path nothing
        # matched the query, and a summary that says otherwise hands the model
        # a fact it has no way to check and every reason to repeat.
        basis = (
            f"matched {query!r}"
            if from_query
            else "came from the shopper's preferred categories"
        )
        return {
            "status": "empty",
            "widget": None,
            "summary": (
                f"{len(candidates)} products {basis}, but none are available "
                "in the shopper's size or clear their material exclusions."
            ),
            "items": [],
        }

    items = []
    for pick in picks:
        product = pick.product
        items.append(
            {
                "id": product["id"],
                "name": product["name"],
                "brand": product["brand"],
                "price": product["price"],
                "image_url": product_tile_uri(product["id"], product["name"]),
                "reasons": pick.reasons,
            }
        )

    stage_widget(
        tool_context.state,
        "picks",
        {
            "headline": _picks_headline(query, profile, from_query),
            "items": items,
            # Kept so a later preference change can re-rank the same request
            # without asking the shopper to repeat it. The converter ignores
            # keys it does not render.
            "query": query.strip(),
        },
    )

    return {
        "status": "ok",
        "widget": "picks",
        "summary": (
            f"Staged {len(items)} picks as cards: "
            + ", ".join(i["name"] for i in items)
        ),
        # Facts, not layout: id and name to refer to an item, price for
        # follow-up arithmetic, and the reason chips so "why this one?" is
        # answerable from the tool result. Withholding the chips looked like
        # a way to stop the model reciting the card -- until a live run showed
        # it answering "why?" by inventing a rationale from another tool's
        # output instead. Not duplicating the widget is the instruction's job;
        # starving the model of facts only buys a fluent guess. The image and
        # brand stay out because they are the card's business.
        "items": [
            {
                "id": i["id"],
                "name": i["name"],
                "price": i["price"],
                "reasons": i["reasons"],
            }
            for i in items
        ],
    }


def compare_picks(
    product_ids: list[str],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Compares products side by side as a table.

    Pass an empty list to compare whatever is currently in the pick
    carousel -- the payload is still in session state, so the shopper does
    not have to name the items again.

    "Best value" is computed as rating per dollar, not judged. Say so if the
    shopper asks why an item carries the flag.

    Args:
      product_ids: Product ids to compare. Empty means "the current picks".

    Returns:
      A summary of what was staged, including the attributes compared.
    """
    ids = [str(i).strip() for i in product_ids if str(i).strip()]
    if not ids:
        ids = _ids_from_picks(tool_context)

    if len(ids) < 2:
        return {
            "status": "empty",
            "widget": None,
            "summary": (
                "A comparison needs at least two products. Ask which items "
                "to compare, or fetch picks first."
            ),
            "attributes": [],
        }

    resolved = []
    unknown = []
    for product_id in ids:
        found = store.product(product_id)
        if found is None:
            unknown.append(product_id)
        else:
            resolved.append(found)

    if len(resolved) < 2:
        return {
            "status": "empty",
            "widget": None,
            "summary": (
                "Could not resolve at least two of those products: "
                + ", ".join(unknown)
            ),
            "attributes": [],
        }

    attributes = _columns(resolved)
    best_id = _best_value_id(resolved)

    items = []
    for product in resolved:
        values: dict[str, Any] = {"Price": product["price"]}
        values.update(product.get("attributes", {}))
        items.append(
            {
                "id": product["id"],
                "name": product["name"],
                "values": {attr: values.get(attr) for attr in attributes},
                "best_value": product["id"] == best_id,
            }
        )

    stage_widget(
        tool_context.state,
        "comparison",
        {
            "headline": "Side by side",
            "attributes": attributes,
            "money_attributes": [a for a in attributes if a in _MONEY_COLUMNS],
            "items": items,
        },
    )

    summary = (
        f"Staged a comparison of {len(items)} products across "
        f"{len(attributes)} attributes."
    )
    if unknown:
        summary += f" Skipped unknown ids: {', '.join(unknown)}."

    return {
        "status": "ok",
        "widget": "comparison",
        "summary": summary,
        "attributes": attributes,
        "best_value": best_id,
    }


@dataclass(frozen=True)
class PicksRefresh:
    """What a re-rank did to the staged pick carousel."""

    cards: int
    """Cards in the carousel after re-ranking. ``0`` if there is none."""

    changed: bool
    """Whether the shopper will see any difference from the re-rank."""

    suppressed: bool
    """Whether the carousel was held back as a byte-identical resend.

    Not the inverse of ``changed``, and the gap between the two is the case
    that matters: a re-rank can come out identical and still ship, when the
    carousel was staged earlier in this same turn and the shopper has not
    seen it yet. The caller needs that difference, because "nothing
    changed" is the wrong thing to say beside a carousel arriving for the
    first time -- and because keying the reported widget off ``changed``
    alone announces no widget in a turn where one goes out.
    """


def restage_picks(tool_context: ToolContext) -> PicksRefresh:
    """Re-ranks the staged pick carousel against the current profile.

    Called after a preference change. This is the interaction inline
    rendering cannot express: ``update_shopper_preference`` refreshes a
    widget that ``get_personalized_picks`` produced, in a turn where the model
    never calls the ranking tool. The ranking itself does re-run -- this
    helper calls it below -- but the shopper never asked for it and never
    repeats their query, because the query was staged alongside the payload.

    A re-rank that changes nothing is suppressed rather than sent -- but only
    when the shopper has already seen the carousel. Republishing a
    byte-identical widget costs a message and, worse, invites the agent to
    announce an update the shopper cannot see, which is the same failure as
    describing a widget that never shipped, in the other direction. When the
    carousel was staged earlier in *this* turn and has not gone out yet there
    is nothing to republish, and suppressing would not spare a resend, it
    would delete the only send: a shopper opening with "I need trail shoes,
    and I'm an XL" would get the confirmation and no cards at all.
    """
    spec = spec_for("picks")
    before = register_payload(tool_context.state, spec)
    # Sampled here, not next to the comparison below, because the re-rank calls
    # ``stage_widget``, which clears both of these flags. Read afterwards this
    # would say "never shipped" for every carousel, including one the shopper
    # has been looking at for three turns.
    #
    # The suppress flag is the second half because ``emitted`` is cleared by
    # that same re-rank and this function can run twice in a turn -- two
    # preference writes in one breath ("I'm an XL and I avoid asbestos"). The
    # first call suppresses and re-stages; a second reading ``emitted`` alone
    # then concludes the carousel was never seen and republishes it
    # byte-identical, which is the failure this whole branch exists to prevent.
    # Consulting the flag cannot resurrect that failure in the other direction,
    # because the flag is only ever set below, on the turn's first call, and
    # only when this same test already passed.
    on_screen = was_emitted(tool_context.state, spec) or is_suppressed(
        tool_context.state, spec
    )
    if not before.get("items"):
        return PicksRefresh(cards=0, changed=False, suppressed=False)

    query = str(before.get("query", ""))
    result = get_personalized_picks(query, tool_context)
    count = len(result.get("items", []))
    if count == 0:
        # The new preferences exclude everything the old carousel held. Drop
        # it rather than leaving a carousel in the register that a later
        # revive would resurrect against a profile it no longer matches.
        clear_staged(tool_context.state, "picks")
        return PicksRefresh(cards=0, changed=False, suppressed=False)

    after = register_payload(tool_context.state, spec)
    if after == before:
        # Compared whole, not by id order: a changed price ceiling can leave
        # the ranking alone and still rewrite every reason chip, and that is
        # a difference worth showing.
        if on_screen:
            suppress_widget(tool_context.state, "picks")
        return PicksRefresh(cards=count, changed=False, suppressed=on_screen)

    return PicksRefresh(cards=count, changed=True, suppressed=False)


def _candidates(
    query: str, profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """Products worth scoring, and whether the query is what found them.

    An empty query means "surprise me", which resolves to the shopper's
    preferred categories rather than the whole catalog -- ranking the full
    catalog against a broad profile mostly surfaces accessories.

    The flag is not bookkeeping. The caller's summary is the model's only
    account of where these products came from, and a summary saying they
    matched a query they never matched is how the agent ends up telling the
    shopper the same thing.
    """
    cleaned = query.strip()
    if cleaned:
        matches = store.find_products(cleaned)
        if matches:
            return matches, True
        # A miss on the literal query is not a dead end: the profile is still
        # a valid basis for a recommendation, and saying "nothing matched" to
        # someone who typed "something for the rain" is unhelpful.
        return _preferred(profile), False
    return _preferred(profile), False


def _preferred(profile: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(profile.get("preferred_categories", []))
    if not wanted:
        return store.products()
    return [p for p in store.products() if p["category"] in wanted]


def _picks_headline(
    query: str, profile: dict[str, Any], from_query: bool
) -> str:
    """A headline stating the actual basis for the picks.

    ``from_query`` rather than "the query is non-empty": a query that missed
    fell back to the profile, and a carousel headed "Matches for X" over
    profile picks is the widget itself making the claim the summary must not.
    """
    if from_query:
        return f"Matches for “{query.strip()}”"
    categories = profile.get("preferred_categories", [])
    if categories:
        readable = " and ".join(c.replace("-", " ") for c in categories[:2])
        return f"From your {readable}"
    return "Picked for you"


def _ids_from_picks(tool_context: ToolContext) -> list[str]:
    """Product ids from the staged pick carousel, if there is one."""
    payload = register_payload(tool_context.state, spec_for("picks"))
    return [
        str(item["id"])
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("id")
    ]


def _columns(products: list[dict[str, Any]]) -> list[str]:
    """Attribute columns, preferred order first then first-seen order."""
    seen: list[str] = []
    for product in products:
        for key in product.get("attributes", {}):
            if key not in seen:
                seen.append(key)
    ordered = [c for c in _PREFERRED_COLUMNS if c == "Price" or c in seen]
    ordered += [c for c in seen if c not in ordered]
    return ordered


def _best_value_id(products: list[dict[str, Any]]) -> str | None:
    """The product with the highest rating per dollar.

    A stated metric rather than an opinion, so the flag on the card means
    something specific and the agent can explain it without inventing a
    rationale. Ties break on id, so the same input always flags the same
    product.
    """
    best_id: str | None = None
    best_ratio = 0.0
    for product in products:
        price = product.get("price")
        rating = product.get("rating")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        if not isinstance(rating, (int, float)):
            continue
        ratio = rating / price
        if ratio > best_ratio or (
            ratio == best_ratio
            and best_id is not None
            and product["id"] < best_id
        ):
            best_ratio = ratio
            best_id = product["id"]
    return best_id
