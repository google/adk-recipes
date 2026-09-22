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
"""What the reply must look like when a widget is on screen.

Staging decides *what the shopper sees*. This decides *what the model is
allowed to say about it* -- and the two are different problems, because the
right reply next to a comparison table is not the right reply next to a
delivery timeline.

The naive fix is one blanket rule in the instruction ("don't repeat the
widget"). It survives exactly as long as every widget wants the same reply.
The second naive fix is a per-widget snippet, which gives N widgets N strings
to keep consistent and lets them contradict each other when two stage at
once.

So this module is a two-level indirection instead:

1. Each widget declares a **role** -- what it is doing for the shopper
   (``spec.presentation_role``).
2. Live roles collapse, by fixed precedence, to exactly one **contract** per
   turn, and each contract owns one canonical instruction.

Three widgets sharing a role therefore share one instruction, and a turn that
stages three widgets still injects one block. Text-primary is the absence of
a contract, not a member of the enum: a turn with no live widget adds nothing
to the instruction, so the ordinary reply rules apply untouched.

The instruction is appended to the *tail* of the system instruction, which is
the position a model weights most heavily for output-shaping directives.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class PresentationRole(StrEnum):
    """What a widget is doing for the shopper this turn.

    Declared per widget in ``staging/spec.py``. Keep this small: a role
    earns its place by changing what the reply should say, not by describing
    the widget's shape.
    """

    # The widget carries the substance of the answer -- cards, a table, a
    # chart. The reply's job is to add judgement the visual cannot show.
    DATA_PRIMARY = "data_primary"

    # The widget is a detail panel next to a direct answer. The question
    # still needs answering in words; the widget holds the specifics.
    SUPPORTING = "supporting"

    # The shopper has seen this exact visual before and the model has
    # already described it. Resolved per turn, not declared -- see
    # ``staging.contract``.
    REPRISE = "reprise"


class PresentationContract(StrEnum):
    """The reply shape for a turn. One at most.

    There is deliberately no ``TEXT_PRIMARY`` member. A turn with no live
    widget resolves to ``None``, and ``None`` means "inject nothing".
    """

    SYNTHESIS = "synthesis"
    ANSWER = "answer"
    ACKNOWLEDGE = "acknowledge"


# Highest precedence first. When several widgets are live in one turn, the
# first role present here wins -- so a freshly staged chart outranks a
# carousel the shopper merely asked to see again, and the reply talks about
# the new thing.
_PRECEDENCE: tuple[tuple[PresentationRole, PresentationContract], ...] = (
    (PresentationRole.DATA_PRIMARY, PresentationContract.SYNTHESIS),
    (PresentationRole.SUPPORTING, PresentationContract.ANSWER),
    (PresentationRole.REPRISE, PresentationContract.ACKNOWLEDGE),
)

# Every contract states the depth bound in words the model can act on ("at
# most three", "a sentence or two") rather than a number it has to count
# tokens against. Each block opens the same way on purpose: it marks the
# boundary between "here is context" and "now write".
_INSTRUCTIONS: dict[PresentationContract, str] = {
    PresentationContract.SYNTHESIS: """\
<presentation_contract>
Write the visible reply now. The shopper can already see the names, prices,
and figures on screen, so do not list them and do not restate them as
bullets. Add at most three things the visual cannot say -- which one to
choose, what the trend means, what changed -- in two or three sentences.
</presentation_contract>""",
    PresentationContract.ANSWER: """\
<presentation_contract>
Write the visible reply now. Answer the question directly in a sentence or
two, then stop. The widget beside your reply carries the dates and detail,
so point at it rather than transcribing it.
</presentation_contract>""",
    PresentationContract.ACKNOWLEDGE: """\
<presentation_contract>
Write the visible reply now. This is a visual the shopper has already seen
and you have already described. One short sentence confirming it is back is
enough -- do not describe its contents again and do not repeat your earlier
reasoning about it.
</presentation_contract>""",
}


def contract_for_roles(
    roles: Iterable[PresentationRole],
) -> PresentationContract | None:
    """The single contract for a turn, or ``None`` for a text-only reply.

    Takes every live widget's role and returns one contract. ``None`` when
    nothing is live, which is the common case and must stay silent: injecting
    a "the widget shows it" instruction on a turn with no widget would tell
    the model to point at something that is not there.
    """
    present = set(roles)
    for role, contract in _PRECEDENCE:
        if role in present:
            return contract
    return None


def instruction_for(contract: PresentationContract | None) -> str:
    """The instruction block for a contract, or ``""`` for none.

    Fails open on an unknown contract: a missing instruction costs reply
    quality, while raising here would cost the shopper the whole turn.
    """
    if contract is None:
        return ""
    return _INSTRUCTIONS.get(contract, "")
