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
"""The staging lifecycle: six gates, a fixed order, and cross-turn revival.

Every test here answers the question this framework gets asked most -- "why
didn't my widget appear?" -- which is why the flush reports an outcome per
widget instead of skipping quietly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.staging import (
    WIDGET_SPECS,
    blocked_emissions,
    clear_staged,
    emit_staged_widgets,
    revive_widget,
    spec_for,
    stage_widget,
    suppress_widget,
)
from app.staging.lifecycle import (
    ALREADY_EMITTED,
    EMITTED,
    EMPTY_REGISTER,
    NOT_STAGED,
    NOTHING_RENDERED,
    RENDER_FAILED,
    SUPPRESSED,
    WIDGET_PROVIDER,
)
from app.staging.state import was_emitted
from conftest import StubContext

PICKS: dict[str, Any] = {
    "headline": "Picked for you",
    "items": [
        {"name": "Cirrus Trail 3", "price": 148.0, "reasons": ["Size 9.5"]}
    ],
}
SPEND: dict[str, Any] = {"points": [{"month": "Jul", "amount": 100.0}]}


def outcomes_by_name(ctx: StubContext) -> dict[str, tuple[bool, str]]:
    return {o.name: (o.emitted, o.reason) for o in emit_staged_widgets(ctx)}


# --- the six gates ----------------------------------------------------------


def test_nothing_staged_emits_nothing_and_writes_nothing(
    ctx: StubContext,
) -> None:
    """A turn with no widgets must not force an event.

    The mirror image of the state-delta trap: writing state unconditionally
    would make every single turn produce an extra event.
    """
    outcomes = outcomes_by_name(ctx)
    assert {
        name for name, (_, reason) in outcomes.items() if reason == NOT_STAGED
    } == {s.name for s in WIDGET_SPECS}
    assert ctx.widgets == []
    assert ctx.state.has_delta() is False


def test_staged_widget_is_emitted_with_a_state_delta(
    ctx: StubContext,
) -> None:
    """The happy path, and the assertion the whole design hangs on.

    Without ``has_delta()``, ``base_agent`` returns ``None`` from the
    after-agent callback and the widget is discarded with no error anywhere.
    """
    stage_widget(ctx.state, "picks", PICKS)
    assert outcomes_by_name(ctx)["picks"] == (True, EMITTED)

    assert ctx.widget_ids == ["ui-picks"]
    widget = ctx.widgets[0]
    assert widget.provider == WIDGET_PROVIDER
    assert widget.payload["type"] == "product_picks"
    assert widget.payload["surfaceId"] == "ui-surface-picks"

    assert ctx.state.has_delta() is True
    assert ctx.state[spec_for("picks").emitted_key] is True


def test_second_flush_in_the_same_turn_is_a_no_op(ctx: StubContext) -> None:
    """Gate 2. Two flushes must not ship the same widget twice."""
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)
    assert outcomes_by_name(ctx)["picks"] == (False, ALREADY_EMITTED)
    assert len(ctx.widgets) == 1


def test_suppressed_widget_is_held_back(ctx: StubContext) -> None:
    """Gate 3. A tool can stage and then decide the turn shouldn't show it."""
    stage_widget(ctx.state, "picks", PICKS)
    suppress_widget(ctx.state, "picks")
    assert outcomes_by_name(ctx)["picks"] == (False, SUPPRESSED)
    assert ctx.widgets == []


def test_the_last_decision_wins_between_staging_and_suppressing(
    ctx: StubContext,
) -> None:
    """Order decides, so a stale veto cannot swallow fresh data.

    Both directions matter and they are easy to get wrong together. Staging
    after a suppression must ship: the suppression was an earlier, less
    informed decision, and leaving it set would report ``suppressed for this
    turn`` for a widget the shopper just got new data for. Suppressing after
    staging must still hold the widget back, because that is the order
    ``tools/picks.py`` uses for a re-rank that changed nothing.
    """
    suppress_widget(ctx.state, "picks")
    stage_widget(ctx.state, "picks", PICKS)
    assert outcomes_by_name(ctx)["picks"] == (True, EMITTED)

    later = ctx.next_turn()
    stage_widget(later.state, "picks", PICKS)
    suppress_widget(later.state, "picks")
    assert outcomes_by_name(later)["picks"] == (False, SUPPRESSED)


def test_reviving_also_beats_an_earlier_suppression(ctx: StubContext) -> None:
    """The same rule, for the other way a widget goes live.

    Staging and reviving are the two writers, and a rule applied to one of
    them is a rule that holds half the time. Here the suppression is the
    stale decision and the revival is the shopper explicitly asking to see
    the carousel again, so the revival wins -- otherwise "bring those back
    up" is answered with ``suppressed for this turn`` and no widget.
    """
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)

    later = ctx.next_turn()
    suppress_widget(later.state, "picks")
    assert revive_widget(later.state, "picks") is True
    assert outcomes_by_name(later)["picks"] == (True, EMITTED)
    assert later.widget_ids == ["ui-picks"]


def test_empty_register_is_reported_not_rendered(ctx: StubContext) -> None:
    """Gate 4. Dirty but empty is a tool bug, and says so."""
    stage_widget(ctx.state, "picks", {})
    assert outcomes_by_name(ctx)["picks"] == (False, EMPTY_REGISTER)


def test_payload_that_renders_empty_is_reported(ctx: StubContext) -> None:
    """Gate 5. A non-empty payload describing nothing to show."""
    stage_widget(ctx.state, "picks", {"headline": "None", "items": []})
    assert outcomes_by_name(ctx)["picks"] == (False, NOTHING_RENDERED)


class RefusingContext(StubContext):
    """A host that rejects one widget id, whatever it is handed.

    ``spec.py`` rejects duplicate ids at import, so the flush's own
    ``ValueError`` path is unreachable through the specs. Refusing from the
    sink is how a test reaches gate 6 -- and a real host is entitled to
    refuse for reasons this recipe cannot see.
    """

    def __init__(self, refuse: str) -> None:
        super().__init__()
        self._refuse = refuse

    def render_ui_widget(self, ui_widget: Any) -> None:
        if ui_widget.id == self._refuse:
            raise ValueError(f"widget id {ui_widget.id} already rendered")
        super().render_ui_widget(ui_widget)


def test_a_refused_widget_is_reported_and_stays_unemitted() -> None:
    """Gate 6. The reply survives the refusal, and so does the register.

    Two things matter here. The other widget still ships -- one rejection must
    not cost the shopper everything else on screen. And the refused widget is
    never marked emitted, so a later turn can revive it rather than treating a
    widget the shopper never saw as already delivered.
    """
    ctx = RefusingContext("ui-picks")
    stage_widget(ctx.state, "picks", PICKS)
    stage_widget(ctx.state, "spend", SPEND)

    outcomes = {o.name: (o.emitted, o.reason) for o in emit_staged_widgets(ctx)}
    assert outcomes["picks"] == (False, RENDER_FAILED)
    assert outcomes["spend"] == (True, EMITTED)
    assert ctx.widget_ids == ["ui-spend"]

    # The register survived the refusal, so a host that accepts it still ships
    # it. A widget the shopper never saw must not read as delivered.
    assert was_emitted(ctx.state, spec_for("picks")) is False
    retry = StubContext(ctx.state.to_dict())
    assert outcomes_by_name(retry)["picks"] == (True, EMITTED)


def test_a_failing_widget_does_not_block_the_others(
    ctx: StubContext,
) -> None:
    """One broken converter costs one widget, never the whole reply."""

    def boom(_payload: Any) -> list[dict[str, Any]]:
        raise RuntimeError("intentional")

    stage_widget(ctx.state, "picks", PICKS)
    stage_widget(ctx.state, "spend", SPEND)
    outcomes = {
        o.name: (o.emitted, o.reason)
        for o in emit_staged_widgets(ctx, overrides={"product_picks": boom})
    }
    assert outcomes["picks"] == (False, NOTHING_RENDERED)
    assert outcomes["spend"] == (True, EMITTED)
    assert ctx.widget_ids == ["ui-spend"]


# --- order and reporting ----------------------------------------------------


def test_emission_order_follows_the_declaration_not_the_staging(
    ctx: StubContext,
) -> None:
    """Widget order is a property of the code, not of the model's tool order.

    Inline rendering emits in whatever sequence the model happened to call
    tools -- so the same question can produce a different layout twice.
    """
    stage_widget(ctx.state, "spend", SPEND)
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)
    assert ctx.widget_ids == ["ui-picks", "ui-spend"]


def test_blocked_emissions_hides_the_widgets_nobody_asked_for(
    ctx: StubContext,
) -> None:
    """Only widgets a tool staged and the flush refused are worth logging."""
    stage_widget(ctx.state, "picks", PICKS)
    suppress_widget(ctx.state, "picks")
    blocked = blocked_emissions(emit_staged_widgets(ctx))
    assert [(b.name, b.reason) for b in blocked] == [("picks", SUPPRESSED)]


def test_staging_an_unknown_widget_fails_loudly(ctx: StubContext) -> None:
    """A typo in a widget name is a bug, not a silently skipped widget.

    The match pins the typo. ``"picks"`` also appears in the list of declared
    names the message ends with, so it would pass even if the bad name were
    never echoed back -- which is the only part a developer needs.
    """
    with pytest.raises(KeyError, match="pickz") as caught:
        stage_widget(ctx.state, "pickz", PICKS)
    # The message also names the real widgets, so a caller can fix the typo.
    assert "picks" in str(caught.value)


def test_widget_ids_and_keys_are_derived_from_one_name() -> None:
    """One spec owns every id and state key, so they cannot drift apart."""
    spec = spec_for("order")
    assert spec.widget_id == "ui-order"
    assert spec.surface_id == "ui-surface-order"
    assert spec.register_key == "ui:register:order"
    assert spec.emitted_key == "ui:emitted:order"
    # Per-invocation flags. Session-scoped ones would leak a suppression or a
    # dirty mark into the next turn.
    assert spec.dirty_key.startswith("temp:")
    assert spec.suppress_key.startswith("temp:")


# --- across turns -----------------------------------------------------------


def test_a_new_turn_does_not_re_emit_last_turn_s_widget(
    ctx: StubContext,
) -> None:
    """The dirty mark is per-invocation, so a quiet turn stays quiet."""
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)

    next_turn = ctx.next_turn()
    assert outcomes_by_name(next_turn)["picks"] == (False, NOT_STAGED)
    assert next_turn.widgets == []


def test_a_widget_can_be_revived_without_recomputing_it(
    ctx: StubContext,
) -> None:
    """The interaction inline rendering cannot express.

    The register is session-scoped, so a later turn can put a widget back on
    screen without the tool that built it running again -- or existing.
    """
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)

    later = ctx.next_turn()
    assert revive_widget(later.state, "picks") is True
    assert outcomes_by_name(later)["picks"] == (True, EMITTED)
    assert later.widgets[0].payload["surfaceId"] == "ui-surface-picks"


def test_reviving_nothing_reports_failure(ctx: StubContext) -> None:
    """No register, no revival -- and the caller is told, not fooled."""
    assert revive_widget(ctx.state, "comparison") is False


def test_restaging_supersedes_an_already_emitted_widget(
    ctx: StubContext,
) -> None:
    """Fresh data beats the dedupe record.

    Otherwise a widget emitted early in a turn could never be corrected by a
    later tool call in the same turn.
    """
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)
    assert len(ctx.widgets) == 1

    updated = ctx.next_turn()
    stage_widget(updated.state, "picks", PICKS)
    assert outcomes_by_name(updated)["picks"] == (True, EMITTED)


def test_clearing_a_register_prevents_a_later_revival(
    ctx: StubContext,
) -> None:
    """Stale data must be droppable, not just hidden.

    ``restage_picks`` relies on this: when new preferences exclude every
    product, the old carousel has to leave the register, or a later revival
    would resurrect picks that no longer match the profile.
    """
    stage_widget(ctx.state, "picks", PICKS)
    clear_staged(ctx.state, "picks")
    assert outcomes_by_name(ctx)["picks"] == (False, NOT_STAGED)
    assert revive_widget(ctx.next_turn().state, "picks") is False
