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
"""The presentation contract: one reply shape per turn, and it must not lie.

The staging tests ask "did the widget ship?". These ask the question that
comes right after it: "was the model told the truth about what shipped?"

The failure this file is mostly built around is not a crash. It is a reply
that says "the chart above shows your spending" on a turn where the chart was
suppressed -- which happens the moment the contract resolver and the flush
disagree about what "live" means. So the mirroring tests assert both halves at
once and would fail if the two ever drifted apart.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.agent import apply_presentation_contract, ensure_widget_companion
from app.presentation import (
    PresentationContract,
    PresentationRole,
    contract_for_roles,
    instruction_for,
)
from app.render.registry import CONVERTERS
from app.staging import (
    WIDGET_SPECS,
    emit_staged_widgets,
    live_specs,
    resolve_contract,
    revive_widget,
    role_for,
    spec_for,
    stage_widget,
    suppress_widget,
)
from conftest import StubContext

PICKS: dict[str, Any] = {
    "headline": "Picked for you",
    "items": [
        {"name": "Cirrus Trail 3", "price": 148.0, "reasons": ["Size 9.5"]}
    ],
}
COMPARISON: dict[str, Any] = {
    "items": [
        {"name": "Cirrus Trail 3", "price": 148.0, "rating": 4.6},
        {"name": "Fell Runner Lite", "price": 132.0, "rating": 4.4},
    ],
    "attributes": ["price", "rating"],
}
ORDER: dict[str, Any] = {
    "order_id": "ORD-4417",
    "status": "In transit",
    "steps": [{"label": "Shipped", "date": "2026-08-14", "done": True}],
}
SPEND: dict[str, Any] = {"points": [{"month": "Jul", "amount": 100.0}]}


def reply(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


# --- what each widget asks of the reply -------------------------------------


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("picks", PICKS, PresentationContract.SYNTHESIS),
        ("comparison", COMPARISON, PresentationContract.SYNTHESIS),
        ("spend", SPEND, PresentationContract.SYNTHESIS),
        # The one that is deliberately different: "where is my order" wants
        # an answer in words, with the timeline as the detail panel.
        ("order", ORDER, PresentationContract.ANSWER),
    ],
)
def test_each_widget_resolves_to_its_declared_contract(
    ctx: StubContext,
    name: str,
    payload: dict[str, Any],
    expected: PresentationContract,
) -> None:
    stage_widget(ctx.state, name, payload)
    assert resolve_contract(ctx.state) is expected


def test_a_turn_with_no_widget_gets_no_contract(ctx: StubContext) -> None:
    """The common case, and the one that must stay silent.

    Text-primary is the absence of a contract rather than a member of the
    enum. If this returned something, every ordinary conversational turn
    would carry an instruction telling the model to point at a widget that
    does not exist.
    """
    assert resolve_contract(ctx.state) is None
    assert instruction_for(None) == ""


# --- collapsing several widgets into one contract ---------------------------


def test_two_widgets_collapse_to_the_higher_precedence_contract(
    ctx: StubContext,
) -> None:
    """One turn, two live widgets, still exactly one reply shape.

    Without a precedence rule this is where per-widget prompt snippets break
    down: the model would receive "answer in a sentence or two" and "add three
    things the visual cannot say" together and have to guess.
    """
    stage_widget(ctx.state, "order", ORDER)
    stage_widget(ctx.state, "picks", PICKS)
    assert len(live_specs(ctx.state)) == 2
    assert resolve_contract(ctx.state) is PresentationContract.SYNTHESIS


def test_fresh_data_outranks_a_revival_in_the_same_turn(
    ctx: StubContext,
) -> None:
    """A reprise plus something new is not a reprise.

    The shopper asked to see the old carousel *and* asked a new question. The
    reply's job is the new thing, so ACKNOWLEDGE must lose here -- otherwise
    the model is told to say one short sentence about a chart it has never
    described.
    """
    stage_widget(ctx.state, "picks", PICKS)
    turn_two = ctx.next_turn()
    assert revive_widget(turn_two.state, "picks")
    stage_widget(turn_two.state, "spend", SPEND)

    assert role_for(turn_two.state, spec_for("picks")) is (
        PresentationRole.REPRISE
    )
    assert resolve_contract(turn_two.state) is PresentationContract.SYNTHESIS


def test_every_contract_is_reachable(ctx: StubContext) -> None:
    """No dead members: each contract is produced by some real turn.

    A contract nobody can reach is a prompt string that will rot unnoticed,
    so this asserts the enum and the widget declarations stay in step.
    """
    reachable = set()
    for spec in WIDGET_SPECS:
        single = StubContext()
        stage_widget(single.state, spec.name, {"anything": True})
        reachable.add(resolve_contract(single.state))

        revived = StubContext({spec.register_key: {"anything": True}})
        assert revive_widget(revived.state, spec.name)
        reachable.add(resolve_contract(revived.state))

    assert reachable == set(PresentationContract)


# The phrase that makes each block the one it is. Asserting the wrapper tags
# alone would pass with all three contracts returning the same text, which
# would collapse the whole point of this module -- three widgets, three reply
# shapes -- without failing anything.
_DISTINGUISHING_PHRASE: dict[PresentationContract, str] = {
    PresentationContract.SYNTHESIS: (
        "at most three things the visual cannot say"
    ),
    PresentationContract.ANSWER: (
        "Answer the question directly in a sentence or"
    ),
    PresentationContract.ACKNOWLEDGE: (
        "One short sentence confirming it is back"
    ),
}


@pytest.mark.parametrize("contract", list(PresentationContract))
def test_every_contract_has_an_instruction(
    contract: PresentationContract,
) -> None:
    block = instruction_for(contract)
    assert block.startswith("<presentation_contract>")
    assert block.endswith("</presentation_contract>")
    assert _DISTINGUISHING_PHRASE[contract] in block


def test_no_two_contracts_share_an_instruction() -> None:
    """Three contracts, three different instructions.

    The parametrized test above pins each block's own phrase; this pins the
    other half, that no block is a copy of another. Together they fail if
    ``instruction_for`` ever returns one shared block for every contract --
    which is the regression that costs the module its reason to exist while
    leaving every mapping test green.
    """
    blocks = [instruction_for(contract) for contract in PresentationContract]

    assert len(set(blocks)) == len(PresentationContract)
    # And each phrase belongs to exactly one block, so the three are not merely
    # distinct by some incidental byte.
    for contract, phrase in _DISTINGUISHING_PHRASE.items():
        owners = [b for b in blocks if phrase in b]
        assert owners == [instruction_for(contract)]


def test_precedence_is_total_over_the_roles() -> None:
    """Every role maps to a contract, so no live widget is ever unaccounted.

    A role missing from the precedence table would silently resolve to
    ``None`` -- a widget on screen and no contract, which is the exact
    mismatch this layer exists to prevent.
    """
    for role in PresentationRole:
        assert contract_for_roles([role]) is not None


# --- the reprise signal -----------------------------------------------------


def test_reviving_is_distinguishable_from_staging(ctx: StubContext) -> None:
    """The reason the revived flag exists at all.

    ``stage_widget`` and ``revive_widget`` otherwise leave identical state, so
    without this flag a revived carousel would be described from scratch a
    second time -- the widget looks the same to the flush either way.
    """
    stage_widget(ctx.state, "picks", PICKS)
    assert resolve_contract(ctx.state) is PresentationContract.SYNTHESIS

    turn_two = ctx.next_turn()
    assert revive_widget(turn_two.state, "picks")
    assert resolve_contract(turn_two.state) is (
        PresentationContract.ACKNOWLEDGE
    )


def test_restaging_after_a_revival_clears_the_reprise(
    ctx: StubContext,
) -> None:
    """New data in the same turn cancels the reprise.

    ``update_shopper_preference`` re-stages the carousel with fresh rankings.
    If the revived flag survived that, the model would be told to acknowledge
    a widget whose contents just changed.
    """
    stage_widget(ctx.state, "picks", PICKS)
    turn_two = ctx.next_turn()
    assert revive_widget(turn_two.state, "picks")
    stage_widget(turn_two.state, "picks", PICKS)

    assert role_for(turn_two.state, spec_for("picks")) is (
        PresentationRole.DATA_PRIMARY
    )
    assert resolve_contract(turn_two.state) is PresentationContract.SYNTHESIS


def test_the_reprise_flag_does_not_survive_the_turn(ctx: StubContext) -> None:
    """It is ``temp:``-scoped, like the other per-turn signals.

    A revived flag that leaked forward would make every later turn about that
    widget an acknowledgement.
    """
    stage_widget(ctx.state, "picks", PICKS)
    turn_two = ctx.next_turn()
    assert revive_widget(turn_two.state, "picks")

    turn_three = turn_two.next_turn()
    assert role_for(turn_three.state, spec_for("picks")) is (
        PresentationRole.DATA_PRIMARY
    )


# --- the resolver and the flush must agree ---------------------------------


def stage_then_suppress(ctx: StubContext) -> None:
    stage_widget(ctx.state, "picks", PICKS)
    suppress_widget(ctx.state, "picks")


def stage_then_emit(ctx: StubContext) -> None:
    stage_widget(ctx.state, "picks", PICKS)
    emit_staged_widgets(ctx)


def stage_nothing_usable(ctx: StubContext) -> None:
    stage_widget(ctx.state, "picks", {})


@pytest.mark.parametrize(
    "setup",
    [stage_then_suppress, stage_then_emit, stage_nothing_usable],
    ids=["suppressed", "already emitted", "empty register"],
)
def test_a_blocked_widget_produces_no_contract(
    ctx: StubContext, setup: Callable[[StubContext], None]
) -> None:
    """Both halves asserted together, which is the whole point.

    Each of these states blocks emission. Each must therefore also produce no
    contract -- because a contract is an instruction to talk about something
    on screen, and nothing is going on screen.

    If the resolver ever grows its own copy of the gates and the two drift,
    this fails on the half that drifted rather than shipping a reply that
    points at a widget the shopper cannot see.
    """
    setup(ctx)

    assert resolve_contract(ctx.state) is None
    assert live_specs(ctx.state) == []

    # A second flush from the same state: whatever the gates say, they say it
    # to both callers.
    probe = StubContext(dict(ctx.state.to_dict()))
    emit_staged_widgets(probe)
    assert probe.widget_ids == []


# --- injecting the instruction ---------------------------------------------


def test_the_contract_lands_at_the_tail_of_the_instruction(
    ctx: StubContext,
) -> None:
    """Appended last, where a model weights output directives most.

    Folded into ``build_instruction`` instead, the block would sit above the
    whole conversation and compete with every other rule in the prompt.
    """
    stage_widget(ctx.state, "picks", PICKS)
    request = LlmRequest()
    request.config.system_instruction = "Be a shopping assistant."

    apply_presentation_contract(ctx, request)  # type: ignore[arg-type]

    instruction = request.config.system_instruction
    assert isinstance(instruction, str)
    assert instruction.startswith("Be a shopping assistant.")
    # Pinned to the block's own text rather than to ``instruction_for(...)``,
    # which is the function the callback itself calls: comparing the two would
    # agree with each other whatever that function returned, including a block
    # for the wrong contract.
    assert instruction.rstrip().endswith("</presentation_contract>")
    tail = instruction[len("Be a shopping assistant.") :]
    assert _DISTINGUISHING_PHRASE[PresentationContract.SYNTHESIS] in tail
    # A carousel is data-primary, so the acknowledge block must not be what
    # landed -- the failure this test is really guarding against is the right
    # position with the wrong contract.
    assert _DISTINGUISHING_PHRASE[PresentationContract.ACKNOWLEDGE] not in tail


def test_a_second_model_call_does_not_duplicate_the_block(
    ctx: StubContext,
) -> None:
    """A turn with two rounds of tool calls reaches the callback twice.

    Repeating the block reads to the model as emphasis it did not earn, and
    grows the prompt for nothing.
    """
    stage_widget(ctx.state, "picks", PICKS)
    request = LlmRequest()

    apply_presentation_contract(ctx, request)  # type: ignore[arg-type]
    apply_presentation_contract(ctx, request)  # type: ignore[arg-type]

    instruction = request.config.system_instruction
    assert isinstance(instruction, str)
    assert instruction.count("<presentation_contract>") == 1


def test_nothing_is_appended_when_no_widget_is_live(ctx: StubContext) -> None:
    """An ordinary turn's prompt is untouched by this layer."""
    request = LlmRequest()
    request.config.system_instruction = "Be a shopping assistant."

    apply_presentation_contract(ctx, request)  # type: ignore[arg-type]

    assert request.config.system_instruction == "Be a shopping assistant."


# --- the floor under an empty reply ----------------------------------------


def test_an_empty_reply_beside_a_live_widget_gets_a_companion(
    ctx: StubContext,
) -> None:
    """The invariant: never a widget with no words beside it.

    The contract tells the model to say less, and a model having an off moment
    can take that to nothing at all. Shaping cannot prevent it; a floor can.
    """
    stage_widget(ctx.state, "picks", PICKS)

    altered = ensure_widget_companion(ctx, reply(""))  # type: ignore[arg-type]

    assert altered is not None
    assert altered.content is not None
    assert altered.content.parts is not None
    assert altered.content.parts[0].text == spec_for("picks").default_companion


def test_two_live_widgets_are_both_captioned(ctx: StubContext) -> None:
    """The floor covers every widget shipping, not just the first one.

    ``ensure_widget_companion`` joins one companion per live spec, so a turn
    that stages a carousel and a chart must not caption only one of them --
    the shopper would be left with an uncaptioned visual, which is the exact
    outcome the floor exists to rule out. Joined in ``WIDGET_SPECS`` order,
    the same order the flush emits in, so the words and the widgets agree.
    """
    stage_widget(ctx.state, "picks", PICKS)
    stage_widget(ctx.state, "spend", SPEND)

    altered = ensure_widget_companion(ctx, reply(""))  # type: ignore[arg-type]

    assert altered is not None
    assert altered.content is not None
    assert altered.content.parts is not None
    text = altered.content.parts[0].text
    assert text == " ".join(
        (
            spec_for("picks").default_companion,
            spec_for("spend").default_companion,
        )
    )


def test_a_reply_with_text_is_left_alone(ctx: StubContext) -> None:
    """The overwhelmingly common path. The floor is a last resort."""
    stage_widget(ctx.state, "picks", PICKS)
    assert (
        ensure_widget_companion(ctx, reply("Three that fit your setup."))  # type: ignore[arg-type]
        is None
    )


def test_an_empty_reply_with_no_widget_is_left_alone(
    ctx: StubContext,
) -> None:
    """Nothing on screen to caption, so there is nothing to say for it."""
    assert ensure_widget_companion(ctx, reply("")) is None  # type: ignore[arg-type]


def test_a_tool_call_response_is_left_alone(ctx: StubContext) -> None:
    """Mid-turn, the model is calling tools rather than replying.

    Its text is legitimately empty here and the visible reply comes on a later
    model call. Replacing this response would drop the tool call.
    """
    stage_widget(ctx.state, "picks", PICKS)
    call = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="compare_picks", args={}
                    )
                )
            ],
        )
    )
    assert ensure_widget_companion(ctx, call) is None  # type: ignore[arg-type]


def test_a_streaming_chunk_is_left_alone(ctx: StubContext) -> None:
    """A partial response is not the finished reply.

    Rewriting one would corrupt the stream and, since text arrives a chunk at
    a time, the first empty chunk would trigger the floor on every turn.
    """
    stage_widget(ctx.state, "picks", PICKS)
    chunk = LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text="")]),
        partial=True,
    )
    assert ensure_widget_companion(ctx, chunk) is None  # type: ignore[arg-type]


def test_an_error_response_is_left_alone(ctx: StubContext) -> None:
    """A failed call must surface as a failure, not as a caption."""
    stage_widget(ctx.state, "picks", PICKS)
    failed = LlmResponse(error_code="RESOURCE_EXHAUSTED")
    assert ensure_widget_companion(ctx, failed) is None  # type: ignore[arg-type]


def test_the_companion_preserves_the_rest_of_the_response(
    ctx: StubContext,
) -> None:
    """Returning a response replaces the original wholesale.

    ``base_llm_flow`` swaps in whatever the callback returns, so anything not
    copied across is lost -- usage metadata included.
    """
    stage_widget(ctx.state, "picks", PICKS)
    original = reply("")
    original.finish_reason = types.FinishReason.STOP

    altered = ensure_widget_companion(ctx, original)  # type: ignore[arg-type]

    assert altered is not None
    assert altered.finish_reason is types.FinishReason.STOP


# --- the declarations themselves -------------------------------------------


@pytest.mark.parametrize("spec", WIDGET_SPECS, ids=lambda s: s.name)
def test_every_widget_declares_how_to_talk_about_it(spec: Any) -> None:
    """A new widget cannot be added without answering both questions."""
    assert isinstance(spec.presentation_role, PresentationRole)
    assert spec.default_companion.strip()
    assert spec.default_companion.endswith(".")


@pytest.mark.parametrize("spec", WIDGET_SPECS, ids=lambda s: s.name)
def test_every_widget_has_a_real_converter(spec: Any) -> None:
    """The unchecked string between a spec and its renderer.

    ``resolve_converter`` falls back to a generic card for an unknown semantic
    type, which is right for a type invented at runtime and wrong for a typo
    in ``WIDGET_SPECS`` -- the chart would quietly become a plain card.
    ``staging.lifecycle`` raises at import if this is violated; this test says
    so out loud, because an import-time check in a module nobody reads is easy
    to delete by accident.
    """
    assert spec.semantic_type in CONVERTERS
