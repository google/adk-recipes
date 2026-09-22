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
"""The six tools: what they stage, and what they hand back to the model.

Two invariants run through the whole file.

*No tool renders.* Each one writes a register and returns a summary; the flush
is the only thing that calls ``render_ui_widget``. ``test_no_tool_renders``
asserts that at the source level, because it is the kind of rule that decays
the first time someone adds a tool in a hurry.

*The summary is thin on purpose.* A tool that returns everything the widget
shows invites the model to recite what the shopper is already looking at.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import app.tools as tools_package
from app import store
from app.catalog import catalog_id, validator
from app.presentation import PresentationContract
from app.profile import PROFILE_KEY, load_profile, update_preference
from app.render.converters import money
from app.render.registry import build_widget
from app.staging import resolve_contract, spec_for
from app.staging.gates import blocking_reason
from app.staging.state import (
    is_dirty,
    is_suppressed,
    mark_emitted,
    register_payload,
)
from app.tools import (
    ALL_TOOLS,
    compare_picks,
    get_order_status,
    get_personalized_picks,
    get_spend_summary,
    show_again,
    update_shopper_preference,
)
from conftest import StubContext


def staged(ctx: StubContext, name: str) -> dict[str, Any]:
    return register_payload(ctx.state, spec_for(name))


def rendered(ctx: StubContext, name: str) -> dict[str, Any]:
    """The staged payload put through its converter, validated as A2UI.

    Staging without rendering would let a tool ship a payload its converter
    cannot use, which is exactly the failure the register hides until flush
    time.
    """
    spec = spec_for(name)
    widget = build_widget(
        spec.semantic_type,
        staged(ctx, name),
        surface_id=spec.surface_id,
        catalog_id=catalog_id(),
    )
    assert widget is not None, f"{name} staged a payload that renders nothing"
    validator().validate(widget["a2ui"])
    return widget


# --- picks ------------------------------------------------------------------


def test_picks_stage_a_renderable_carousel(ctx: StubContext) -> None:
    result = get_personalized_picks("trail shoes", ctx)

    assert result["status"] == "ok"
    assert result["widget"] == "picks"
    payload = staged(ctx, "picks")
    assert 1 <= len(payload["items"]) <= 3
    rendered(ctx, "picks")


def test_picks_carry_computed_reasons_and_offline_images(
    ctx: StubContext,
) -> None:
    """Chips come from the ranking; tiles are generated, not fetched.

    A recipe that pointed at real product photos would show broken images on
    the first clone, so the tiles are SVG data URIs computed from the product
    id.
    """
    get_personalized_picks("trail shoes", ctx)
    item = staged(ctx, "picks")["items"][0]

    assert item["reasons"], "ranking produced no reason chips"
    assert item["image_url"].startswith("data:image/svg+xml;base64,")


def test_the_summary_carries_facts_and_leaves_out_layout(
    ctx: StubContext,
) -> None:
    """The model gets what it must reason about; the card keeps the rest.

    Reason chips are facts -- they are how "why this one?" gets a truthful
    answer -- so they are returned. The image is not: it is the card's
    business, and a data URI in a tool result is thousands of wasted tokens.
    """
    result = get_personalized_picks("trail shoes", ctx)
    assert set(result["items"][0]) == {"id", "name", "price", "reasons"}

    staged_item = staged(ctx, "picks")["items"][0]
    assert {"image_url", "brand"} <= set(staged_item)
    assert result["items"][0]["reasons"] == staged_item["reasons"]


def test_an_empty_query_recommends_from_the_profile(ctx: StubContext) -> None:
    """ "What should I get" is a real request, not a missing argument."""
    result = get_personalized_picks("", ctx)
    assert result["status"] == "ok"
    categories = set(load_profile(ctx.state)["preferred_categories"])
    for item in result["items"]:
        assert store.product(item["id"])["category"] in categories


def test_an_unmatchable_query_still_recommends(ctx: StubContext) -> None:
    """A literal miss falls back to the profile rather than dead-ending."""
    result = get_personalized_picks("zzzz no such product", ctx)
    assert result["status"] == "ok"
    assert result["items"]
    # The carousel must not head profile picks as matches for a query that
    # matched nothing -- that is the widget making the false claim.
    assert "Matches for" not in staged(ctx, "picks")["headline"]


def test_the_fallback_does_not_report_profile_picks_as_matches(
    ctx: StubContext,
) -> None:
    """A summary that invents a match is a lie the model cannot detect.

    The instruction tells the model to stay inside the tool results, so a tool
    result claiming products matched a query they never matched is spoken to
    the shopper as fact. Sizes nothing is stocked in, to reach the branch that
    describes the candidate set.
    """
    update_preference(ctx.state, "shoe_size", "15")
    update_preference(ctx.state, "apparel_size", "XXXL")
    result = get_personalized_picks("zzzz no such product", ctx)

    assert result["status"] == "empty"
    assert "matched" not in result["summary"]
    assert "preferred categories" in result["summary"]


# --- comparison -------------------------------------------------------------


def test_comparison_needs_two_products(ctx: StubContext) -> None:
    result = compare_picks(["cirrus-trail-3"], ctx)
    assert result["status"] == "empty"
    assert result["widget"] is None
    assert "at least two" in result["summary"]


def test_comparison_defaults_to_the_cards_on_screen(
    ctx: StubContext,
) -> None:
    """The interaction that makes the register worth keeping.

    "Which of those is better" names nothing, and does not have to: the picks
    are still in session state.
    """
    picks = get_personalized_picks("trail shoes", ctx)
    result = compare_picks([], ctx)

    assert result["status"] == "ok"
    compared = {item["id"] for item in staged(ctx, "comparison")["items"]}
    assert compared == {item["id"] for item in picks["items"]}
    rendered(ctx, "comparison")


def test_comparison_marks_exactly_one_best_value(ctx: StubContext) -> None:
    """A stated metric -- rating per dollar -- not a judgement."""
    ids = ["cirrus-trail-3", "fell-runner-lite", "haldenshell-ultra"]
    result = compare_picks(ids, ctx)

    flagged = [i for i in staged(ctx, "comparison")["items"] if i["best_value"]]
    assert len(flagged) == 1
    assert flagged[0]["id"] == result["best_value"]

    products = [store.product(i) for i in ids]
    expected = max(products, key=lambda p: p["rating"] / p["price"])
    assert result["best_value"] == expected["id"]


def test_comparison_price_column_comes_first_and_is_money(
    ctx: StubContext,
) -> None:
    compare_picks(["cirrus-trail-3", "fell-runner-lite"], ctx)
    payload = staged(ctx, "comparison")
    assert payload["attributes"][0] == "Price"
    assert payload["money_attributes"] == ["Price"]


def test_comparison_skips_unknown_ids_without_failing(
    ctx: StubContext,
) -> None:
    """Two real ids and a hallucinated one still produce a table."""
    result = compare_picks(
        ["cirrus-trail-3", "fell-runner-lite", "no-such-sku"], ctx
    )
    assert result["status"] == "ok"
    assert "no-such-sku" in result["summary"]
    assert len(staged(ctx, "comparison")["items"]) == 2


def test_comparison_of_only_unknown_ids_stages_nothing(
    ctx: StubContext,
) -> None:
    result = compare_picks(["no-such-sku", "also-not-real"], ctx)
    assert result["status"] == "empty"
    assert staged(ctx, "comparison") == {}


# --- orders -----------------------------------------------------------------


def test_order_timeline_has_exactly_one_current_step(
    ctx: StubContext,
) -> None:
    """Derived from position, so it cannot contradict itself."""
    result = get_order_status("ORD-4417", ctx)
    assert result["status"] == "ok"

    steps = staged(ctx, "order")["steps"]
    assert [s["state"] for s in steps].count("current") == 1
    rendered(ctx, "order")


def test_only_the_first_upcoming_step_carries_the_eta(
    ctx: StubContext,
) -> None:
    """Otherwise every remaining stage claims to land on the same day."""
    get_order_status("ORD-4417", ctx)
    upcoming = [
        s for s in staged(ctx, "order")["steps"] if s["state"] == "upcoming"
    ]
    with_eta = [s for s in upcoming if "Expected" in s["detail"]]
    assert len(with_eta) == 1
    assert with_eta[0] is upcoming[0]


def test_a_delivered_order_has_no_current_step(ctx: StubContext) -> None:
    """Nothing is in progress once the last stage is reached."""
    get_order_status("ORD-4388", ctx)
    states = [s["state"] for s in staged(ctx, "order")["steps"]]
    assert set(states) == {"done"}


def test_a_pinned_problem_state_wins_over_position(
    ctx: StubContext,
) -> None:
    """ "Needs attention" cannot be derived from where a stage sits."""
    result = get_order_status("ORD-4402", ctx)
    assert result["needs_attention"] is True
    assert "problem" in [s["state"] for s in staged(ctx, "order")["steps"]]
    rendered(ctx, "order")


def test_a_cancelled_order_renders_as_cancelled(ctx: StubContext) -> None:
    """The result the model reads has to agree with the timeline beside it."""
    result = get_order_status("ORD-4351", ctx)
    assert "cancelled" in [s["state"] for s in staged(ctx, "order")["steps"]]
    rendered(ctx, "order")

    # Reported as cancelled rather than as finished. This order is cancelled
    # at its *last* stage, so the assertion cannot separate the pinned-state
    # lookup from the last-stage fallback -- the next test does that.
    assert result["current_step"] == "Cancelled"
    assert "delivered" not in result["summary"].lower()


def test_a_cancellation_mid_timeline_is_not_reported_as_delivered(
    ctx: StubContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported bug, in the one shape the fixtures cannot express.

    Every cancelled order in the fixtures is cancelled at its last stage, so
    the last-stage fallback returns "Cancelled" there whether or not the
    lookup honours the pinned state. An order halted with stages still ahead
    of it is what tells the two apart: keyed off "current" alone it reports
    the final stage, which is the delivery that never happened.
    """
    halted = {
        "id": "ORD-4210",
        "item_ids": [],
        "stages": [
            {"label": "Ordered", "on": "2026-05-01", "reached": True},
            {
                "label": "Cancelled",
                "on": "2026-05-02",
                "reached": True,
                "state": "cancelled",
            },
            {"label": "Out for delivery", "reached": False},
            {"label": "Delivered", "reached": False},
        ],
    }
    monkeypatch.setattr(store, "order", lambda _order_id: halted)

    result = get_order_status("ORD-4210", ctx)
    assert result["current_step"] == "Cancelled"
    assert "Cancelled" in result["summary"]
    # Settled, so there is nothing for the shopper to do about it.
    assert result["needs_attention"] is False


def test_a_finished_order_stands_at_its_last_stage(ctx: StubContext) -> None:
    """A completed order is where it ended, and needs nothing."""
    result = get_order_status("ORD-4388", ctx)
    assert result["current_step"] == "Delivered"
    assert result["needs_attention"] is False


def test_the_last_stage_is_read_from_the_data_not_named(
    ctx: StubContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback reads the timeline; it does not assume a delivery.

    Every finished order in the fixtures ends on "Delivered", so the test
    above passes just as well against the hardcoded literal this replaced.
    An order collected in store is the case that tells the two apart.
    """
    collected = {
        "id": "ORD-4200",
        "item_ids": [],
        "stages": [
            {"label": "Ordered", "on": "2026-04-01", "reached": True},
            {"label": "Ready for pickup", "on": "2026-04-02", "reached": True},
            {
                "label": "Collected in store",
                "on": "2026-04-03",
                "reached": True,
            },
        ],
    }
    monkeypatch.setattr(store, "order", lambda _order_id: collected)

    result = get_order_status("ORD-4200", ctx)
    assert result["current_step"] == "Collected in store"
    # The summary carries it to the model, so it has to agree.
    assert "Collected in store" in result["summary"]


def test_an_order_with_no_stages_says_so(
    ctx: StubContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No timeline, no answer -- and no invented one."""
    monkeypatch.setattr(
        store, "order", lambda _order_id: {"id": "ORD-4100", "stages": []}
    )
    assert get_order_status("ORD-4100", ctx)["current_step"] == "Unknown"


def test_an_empty_order_id_finds_the_open_one(ctx: StubContext) -> None:
    """The literal, not ``latest_open_order()``, which is the code under test.

    Two orders in the fixtures are still moving, so "the open one" is a choice
    between them. Asserting against the accessor would agree with itself if
    that choice ever reversed.
    """
    result = get_order_status("", ctx)
    assert result["status"] == "ok"
    assert result["order_id"] == "ORD-4417"


def test_order_ids_are_matched_case_insensitively(ctx: StubContext) -> None:
    assert get_order_status("ord-4417", ctx)["order_id"] == "ORD-4417"


def test_an_unknown_order_lists_the_real_ones(ctx: StubContext) -> None:
    """The agent needs something to offer, not just a refusal."""
    result = get_order_status("ORD-9999", ctx)
    assert result["status"] == "not_found"
    assert result["widget"] is None
    assert "ORD-4417" in result["summary"]
    assert staged(ctx, "order") == {}


# --- spend ------------------------------------------------------------------


def test_spend_chart_and_summary_come_from_one_series(
    ctx: StubContext,
) -> None:
    """The headline average and the plotted line cannot disagree.

    They are computed from the same list -- which is the entire argument for
    not letting a model narrate a chart it did not draw.
    """
    result = get_spend_summary(6, ctx)
    payload = staged(ctx, "spend")

    amounts = [p["amount"] for p in payload["points"]]
    assert len(amounts) == 6
    assert result["total"] == pytest.approx(round(sum(amounts), 2))
    assert result["average"] == pytest.approx(
        round(sum(amounts) / len(amounts), 2)
    )
    assert payload["note"] == f"Averaging {money(result['average'])} a month"
    rendered(ctx, "spend")


def test_spend_reports_the_highest_month_from_the_window(
    ctx: StubContext,
) -> None:
    result = get_spend_summary(6, ctx)
    points = staged(ctx, "spend")["points"]
    peak = max(points, key=lambda p: p["amount"])
    assert result["highest_month"] == peak["month"]
    assert result["highest_amount"] == peak["amount"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(0, 6), (-3, 6), (1, 1), (12, 12), (99, 12)],
)
def test_the_spend_window_is_clamped_not_rejected(
    ctx: StubContext, requested: int, expected: int
) -> None:
    """A shopper asking about spending deserves a chart, not an error."""
    result = get_spend_summary(requested, ctx)
    assert result["months"] == expected
    assert len(staged(ctx, "spend")["points"]) == expected


def test_spend_months_are_labelled_as_names(ctx: StubContext) -> None:
    get_spend_summary(3, ctx)
    labels = [p["month"] for p in staged(ctx, "spend")["points"]]
    assert all(len(label) == 3 for label in labels), labels


# --- preferences, and the cross-tool refresh --------------------------------


def test_a_preference_change_refreshes_the_cards_on_screen(
    ctx: StubContext,
) -> None:
    """The interaction that inline rendering cannot express.

    The model does not call ``get_personalized_picks`` this turn, yet its
    widget is re-ranked and re-staged -- because the payload and the original
    query are in session state, so the preference tool can run the ranking
    itself without the shopper repeating anything.
    """
    get_personalized_picks("shoes", ctx)
    before = [i["id"] for i in staged(ctx, "picks")["items"]]

    result = update_shopper_preference("favorite_brands", "Fellstone", ctx)

    assert result["status"] == "ok"
    assert result["widget"] == "picks"
    assert result["picks_refreshed"] > 0
    after = [i["id"] for i in staged(ctx, "picks")["items"]]
    assert after != before, "a new favourite brand should reorder the carousel"
    rendered(ctx, "picks")


def test_a_change_that_reranks_nothing_leaves_the_carousel_alone(
    ctx: StubContext,
) -> None:
    """An identical re-rank is suppressed, not republished.

    Setting the size the shopper already wears changes no ranking and no
    chip, so the carousel would go out byte-identical -- and the agent would
    announce an update the shopper cannot see.
    """
    get_personalized_picks("trail shoes", ctx)
    mark_emitted(ctx.state, spec_for("picks"))
    # A later turn, because "already on screen" is the whole premise: the
    # carousel has to have gone out for resending it to be the waste this
    # test is about. The register survives the turn boundary; the temp flags
    # do not, which is what makes the suppression below this turn's decision.
    later = ctx.next_turn()
    before = staged(later, "picks")
    stored_size = load_profile(later.state)["shoe_size"]

    result = update_shopper_preference("shoe_size", stored_size, later)

    assert result["status"] == "ok"
    assert result["widget"] is None
    assert result["picks_refreshed"] == 0
    assert "unchanged" in result["summary"]
    # The payload stays put; only this turn's emission is vetoed.
    assert staged(later, "picks") == before
    assert is_suppressed(later.state, spec_for("picks"))


def test_a_change_in_the_turn_that_built_the_carousel_still_ships_it(
    ctx: StubContext,
) -> None:
    """The mirror case, where a veto would delete the only send.

    "I need trail shoes, and I'm an XL" is one turn and two tools. The size
    changes no ranking, so the re-rank comes out identical -- but the
    carousel has not gone out yet, so suppressing it spares the shopper
    nothing and costs them every card. Asserted through ``blocking_reason``
    rather than just the flag, because what matters is that the flush would
    still ship it.

    The result the model reads is asserted alongside the state, because
    getting one right is not getting the other right: this shipped a
    carousel while telling the model no widget arrived and that the cards
    were "already on screen" -- unseen cards, described as unchanged, in the
    same turn the contract was asking for a reply written around them.
    """
    get_personalized_picks("trail shoes", ctx)
    before = staged(ctx, "picks")

    result = update_shopper_preference("apparel_size", "XL", ctx)

    assert result["status"] == "ok"
    assert staged(ctx, "picks") == before, "the re-rank must come out identical"
    assert not is_suppressed(ctx.state, spec_for("picks"))
    assert blocking_reason(ctx.state, spec_for("picks")) is None
    rendered(ctx, "picks")

    # What the model is told, against what the turn actually does.
    assert result["widget"] == "picks", "a carousel ships this turn"
    assert "already on screen" not in result["summary"]
    assert resolve_contract(ctx.state) is PresentationContract.SYNTHESIS


def test_two_no_op_changes_in_one_turn_still_suppress_the_resend(
    ctx: StubContext,
) -> None:
    """One turn, two preference writes -- the suppression must survive both.

    "I'm an XL and I avoid asbestos" is ordinary phrasing, and neither field
    can move a trail-shoe ranking. The trap is that the emitted flag is
    per-turn state and the first call's re-rank clears it: read again, it says
    the carousel was never seen, and the second call republishes byte-identical
    cards the shopper has been looking at since last turn. Suppression that
    holds for one call and not two is not suppression.

    Both results are asserted, not just the last, because the failure showed
    up as two tool results describing the same carousel in opposite terms in
    the same turn -- one saying it was unchanged and on screen, the next saying
    it was arriving with this reply.
    """
    get_personalized_picks("trail shoes", ctx)
    mark_emitted(ctx.state, spec_for("picks"))
    later = ctx.next_turn()
    before = staged(later, "picks")

    first = update_shopper_preference("apparel_size", "XL", later)
    second = update_shopper_preference("avoid_materials", "asbestos", later)

    assert staged(later, "picks") == before, "neither field re-ranks these"
    assert is_suppressed(later.state, spec_for("picks"))
    assert blocking_reason(later.state, spec_for("picks")) == (
        "suppressed for this turn"
    )
    assert resolve_contract(later.state) is None
    for result in (first, second):
        assert result["status"] == "ok"
        assert result["widget"] is None, "nothing ships, so nothing is claimed"
        assert "unchanged" in result["summary"]


def test_a_real_rerank_then_a_no_op_change_still_ships_the_carousel(
    ctx: StubContext,
) -> None:
    """The same turn shape, but the first change genuinely re-ranks.

    Here the carousel *must* go out: it no longer matches what the shopper is
    looking at. This is the case that rules out simply remembering the emitted
    flag from the start of the turn -- that reading suppresses on the second
    call and ships nothing, losing a re-rank the shopper asked for.

    The second summary is asserted for what it must *not* say. It cannot tell
    that an earlier call re-ranked these cards, so it states only that the
    arriving cards reflect the change; telling the model not to call them an
    update would contradict the first result in the same turn.
    """
    get_personalized_picks("trail shoes", ctx)
    mark_emitted(ctx.state, spec_for("picks"))
    later = ctx.next_turn()
    on_screen = staged(later, "picks")

    first = update_shopper_preference("favorite_brands", "Fellstone", later)
    second = update_shopper_preference("avoid_materials", "asbestos", later)

    assert staged(later, "picks") != on_screen, "the brand must re-rank these"
    assert not is_suppressed(later.state, spec_for("picks"))
    assert blocking_reason(later.state, spec_for("picks")) is None
    assert resolve_contract(later.state) is PresentationContract.SYNTHESIS
    rendered(later, "picks")

    assert first["widget"] == "picks"
    assert first["picks_refreshed"] > 0
    assert second["widget"] == "picks", "the carousel still ships"
    assert "unchanged" not in second["summary"]
    assert "not call them an update" not in second["summary"]


def test_a_preference_change_with_nothing_on_screen_is_still_stored(
    ctx: StubContext,
) -> None:
    result = update_shopper_preference("needs_waterproof", "yes", ctx)
    assert result["status"] == "ok"
    assert result["widget"] is None
    assert result["picks_refreshed"] == 0
    assert ctx.state[PROFILE_KEY]["needs_waterproof"] is True


def test_a_refused_preference_offers_the_editable_fields(
    ctx: StubContext,
) -> None:
    """The model's next move should be a valid field, not the same value."""
    result = update_shopper_preference("price_ceiling", "cheapish", ctx)
    assert result["status"] == "rejected"
    assert "price_ceiling" in result["editable_fields"]
    assert PROFILE_KEY not in ctx.state


def test_preferences_survive_into_a_later_turn(ctx: StubContext) -> None:
    """``user:`` scope, demonstrated end to end."""
    update_shopper_preference("shoe_size", "11", ctx)
    assert load_profile(ctx.next_turn().state)["shoe_size"] == "11"


def test_a_preference_that_excludes_everything_clears_the_register(
    ctx: StubContext,
) -> None:
    """Otherwise a later revival resurrects picks the profile now rejects."""
    get_personalized_picks("trail shoes", ctx)
    assert staged(ctx, "picks")["items"]

    result = update_shopper_preference("shoe_size", "22", ctx)

    assert result["picks_refreshed"] == 0
    assert staged(ctx, "picks") == {}


# --- showing something again -------------------------------------------------


def test_show_again_revives_the_exact_payload_without_recomputing(
    ctx: StubContext,
) -> None:
    """The claim inline rendering cannot make at all.

    Two turns later, with the producing tool never called again, the same
    carousel ships -- and it is the same object, not a fresh search that
    happens to look similar.
    """
    get_personalized_picks("trail shoes", ctx)
    original = staged(ctx, "picks")

    later = ctx.next_turn().next_turn()
    result = show_again("those shoe cards", later)

    assert result["status"] == "ok"
    assert result["widget"] == "picks"
    assert staged(later, "picks") == original
    assert is_dirty(later.state, spec_for("picks"))


def test_show_again_ships_the_stored_carousel_not_a_fresh_search(
    ctx: StubContext,
) -> None:
    """Why this is a tool and not an instruction to search again.

    The profile has moved on since the carousel was built, so the same query
    now ranks differently. "Show me those again" means the cards the shopper
    is remembering -- reviving returns those; re-searching returns other
    products under the same headline.
    """
    get_personalized_picks("shoes", ctx)
    original = [i["id"] for i in staged(ctx, "picks")["items"]]

    later = ctx.next_turn()
    update_preference(later.state, "favorite_brands", "Fellstone")

    assert show_again("picks", later)["status"] == "ok"
    assert [i["id"] for i in staged(later, "picks")["items"]] == original

    # The contrast that makes the revival worth its own tool.
    fresh = get_personalized_picks("shoes", later)
    assert [i["id"] for i in fresh["items"]] != original


@pytest.mark.parametrize(
    ("phrasing", "expected"),
    [
        ("picks", "picks"),
        ("the product cards", "picks"),
        ("bring those recommendations back", "picks"),
        ("that comparison", "comparison"),
        ("the side by side table", "comparison"),
        ("my delivery", "order"),
        ("order timeline", "order"),
        ("the spending chart", "spend"),
        ("what I spent", "spend"),
    ],
)
def test_show_again_understands_how_shoppers_say_it(
    ctx: StubContext, phrasing: str, expected: str
) -> None:
    """The model paraphrases; the tool should not need the exact word."""
    get_personalized_picks("trail shoes", ctx)
    compare_picks([], ctx)
    get_order_status("", ctx)
    get_spend_summary(6, ctx)

    assert show_again(phrasing, ctx.next_turn())["widget"] == expected


def test_show_again_with_nothing_stored_offers_what_there_is(
    ctx: StubContext,
) -> None:
    """A refusal the agent can act on beats a refusal it has to apologise for."""
    get_personalized_picks("trail shoes", ctx)

    result = show_again("the comparison", ctx.next_turn())

    assert result["status"] == "empty"
    assert result["widget"] is None
    assert result["available"] == ["picks"]
    assert "product cards" in result["summary"]


def test_show_again_on_an_empty_conversation_says_so(
    ctx: StubContext,
) -> None:
    result = show_again("picks", ctx)
    assert result["status"] == "empty"
    assert result["available"] == []
    assert "Nothing has been shown yet" in result["summary"]


def test_show_again_rejects_what_it_cannot_show(ctx: StubContext) -> None:
    """Better than reviving the wrong widget on a fuzzy match."""
    result = show_again("my loyalty points", ctx)
    assert result["status"] == "rejected"
    assert result["widget"] is None
    assert "picks, comparison, order, spend" in result["summary"]


def test_show_again_lists_what_is_available_on_every_path(
    ctx: StubContext,
) -> None:
    """Including success -- "and the comparison too" is the common follow-up."""
    get_personalized_picks("trail shoes", ctx)
    get_spend_summary(3, ctx)

    result = show_again("picks", ctx.next_turn())

    assert result["status"] == "ok"
    # Emission order, not the order the tools ran in.
    assert result["available"] == ["picks", "spend"]


# --- the invariant ----------------------------------------------------------


def test_every_tool_is_registered(ctx: StubContext) -> None:
    """A tool the agent cannot call is a tool nobody notices is broken."""
    assert len(ALL_TOOLS) == 6
    assert {tool.__name__ for tool in ALL_TOOLS} == {
        "get_personalized_picks",
        "compare_picks",
        "get_order_status",
        "get_spend_summary",
        "update_shopper_preference",
        "show_again",
    }


def test_no_tool_renders() -> None:
    """No tool calls ``render_ui_widget``; only the flush does.

    The rule the layout exists to enforce, checked at the source level because
    the alternative is noticing the day two tools race to render the same id.

    Scoped to all of ``app/`` rather than this package, because that is the
    claim the README and ``app/tools/__init__.py`` both make: a render call
    added under ``app/render/`` or ``app/staging/`` clears a tools-only glob
    while falsifying the sentence. Asserted as an equality, so deleting the
    one legitimate call fails too -- a flush that renders nothing emits no
    widgets at all, and every staging test would still pass.

    Matched on the attribute form, which is what separates a call from the two
    places that only *define* the method: the Protocol in ``lifecycle.py`` and
    the walkthrough's ``DemoContext``. Prose mentions carry no parenthesis, so
    the docstrings explaining the rule are not themselves violations of it.
    """
    app_root = Path(tools_package.__file__).parent.parent
    call_sites = sorted(
        str(path.relative_to(app_root))
        for path in app_root.rglob("*.py")
        if re.search(
            r"\.render_ui_widget\s*\(", path.read_text(encoding="utf-8")
        )
    )
    assert call_sites == ["staging/lifecycle.py"]


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.__name__)
def test_every_tool_documents_itself_for_the_model(tool: Any) -> None:
    """ADK sends these docstrings to the model as the tool declaration.

    A tool with no ``Args:`` section is a tool the model calls with guessed
    arguments.
    """
    doc = tool.__doc__ or ""
    assert doc.strip()
    assert "Args:" in doc
    assert "Returns:" in doc
