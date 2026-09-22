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
"""See the widgets without a model, a key, or a network connection.

    uv run python -m app.walkthrough

Calls the tools in the order a conversation would, flushes after each turn,
and prints what the client receives: the staging outcome per widget, and with
``--a2ui`` the A2UI messages themselves. Everything below the model is
exercised -- ranking, converters, the gates, the surface assembly -- because
the only thing this script stands in for is the model's choice of tool.

Useful for four things: reading real A2UI without setting up a host, seeing
which gate held a widget back, watching turn 4 re-stage a carousel without the
model calling a pick tool, and reading the presentation contract each turn
resolves to -- turn 3 asks for an ``answer`` because a delivery timeline is a
detail panel, and turn 6 drops to ``acknowledge`` because the shopper has seen
that carousel already.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterator
from typing import Any

from google.adk.sessions.state import State

from .profile import load_profile
from .staging import (
    blocked_emissions,
    emit_staged_widgets,
    live_specs,
    resolve_contract,
)
from .tools import (
    compare_picks,
    get_order_status,
    get_personalized_picks,
    get_spend_summary,
    show_again,
    update_shopper_preference,
)

# Long enough to prove the tile is a real SVG, short enough to read.
_URI_PREVIEW = 48


class DemoContext:
    """The two members of ADK's context the staging layer touches.

    Real ``State``, so the delta that forces the event out is visible here
    too: a turn that stages nothing leaves ``has_delta()`` false, and that is
    exactly why the flush must not write state unconditionally.

    The sink reproduces ADK's duplicate-id guard
    (``agents/context.py:1010``), so a demo run is as strict about a repeated
    widget id as a real host is. No turn below trips it, and none should: the
    point is that a change to the flush which shipped one widget twice would
    fail here rather than print happily and fail in front of a client.
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state = State(value=dict(state or {}), delta={})
        self.widgets: list[Any] = []

    def render_ui_widget(self, ui_widget: Any) -> None:
        if any(widget.id == ui_widget.id for widget in self.widgets):
            raise ValueError(f"widget id {ui_widget.id} already rendered")
        self.widgets.append(ui_widget)

    def next_turn(self) -> DemoContext:
        """A fresh context carrying only what ADK carries between turns.

        ``temp:`` keys are per-invocation, so they are dropped -- which is
        what makes the suppression and dirty flags in this walkthrough behave
        the way they do under a runner. A new object rather than a mutation
        because ADK's ``State`` has no key deletion.
        """
        return DemoContext(
            {
                key: value
                for key, value in self.state.to_dict().items()
                if not key.startswith("temp:")
            }
        )


Turn = tuple[str, Callable[[DemoContext], dict[str, Any]]]

TURNS: list[Turn] = [
    (
        "I need new trail shoes",
        lambda ctx: get_personalized_picks("trail shoes", ctx),
    ),
    (
        "which of those is the best value?",
        # Empty list: the ids come from the register, not from the shopper.
        lambda ctx: compare_picks([], ctx),
    ),
    (
        "where is my order?",
        lambda ctx: get_order_status("", ctx),
    ),
    (
        "actually I only buy Fellstone now",
        # The model calls no pick tool this turn -- yet the carousel refreshes.
        lambda ctx: update_shopper_preference(
            "favorite_brands", "Fellstone", ctx
        ),
    ),
    (
        "and my shoe size hasn't changed, by the way",
        # A re-rank that comes out identical: staged, then held back, because
        # republishing it would let the reply claim an invisible update.
        lambda ctx: update_shopper_preference(
            "shoe_size", load_profile(ctx.state)["shoe_size"], ctx
        ),
    ),
    (
        "bring those shoe cards back up",
        # Nothing is computed here. The register still holds the carousel that
        # turn 4 staged -- turn 5 re-ranked and wrote the same bytes -- and
        # reviving it flips four flags.
        lambda ctx: show_again("those shoe cards", ctx),
    ),
    (
        "how much have I been spending?",
        lambda ctx: get_spend_summary(6, ctx),
    ),
    (
        "where's order 9999?",
        # Nothing to show: the tool answers in words and stages no widget.
        lambda ctx: get_order_status("ORD-9999", ctx),
    ),
]


def redact_uris(value: Any) -> Any:
    """Shorten generated data URIs so the JSON stays readable."""
    if isinstance(value, dict):
        return {k: redact_uris(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_uris(v) for v in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return f"{value[:_URI_PREVIEW]}... ({len(value)} chars)"
    return value


def walk(*, show_a2ui: bool) -> Iterator[str]:
    """Each line of the walkthrough, in order."""
    ctx = DemoContext()
    for number, (utterance, call) in enumerate(TURNS, start=1):
        if number > 1:
            ctx = ctx.next_turn()
        yield f"\n=== turn {number}: {utterance}"

        result = call(ctx)
        yield f"    tool returned: {result['summary']}"

        # Resolved before the flush, which is where ``before_model_callback``
        # resolves it too. It has to be: emitting sets the emitted flag, and a
        # widget that has already gone out is no longer live.
        contract = resolve_contract(ctx.state)
        if contract is None:
            yield "    contract:      none -- an ordinary text reply"
        else:
            live = ", ".join(spec.name for spec in live_specs(ctx.state))
            yield f"    contract:      {contract.value} (for: {live})"

        outcomes = emit_staged_widgets(ctx)
        for held in blocked_emissions(outcomes):
            yield f"    held back:     {held.name} ({held.reason})"
        if not ctx.widgets:
            yield "    widgets:       none -- the reply carries this turn"
        for widget in ctx.widgets:
            components = widget.payload["a2ui"][1]["updateComponents"][
                "components"
            ]
            yield (
                f"    widget:        {widget.id} "
                f"({widget.payload['type']}, {len(components)} components)"
            )
            if show_a2ui:
                yield json.dumps(redact_uris(widget.payload["a2ui"]), indent=2)

        # The write that makes ADK emit the event at all. Without it an
        # after-agent callback that only renders produces no event and the
        # widgets are dropped -- see the README.
        yield f"    state delta:   {ctx.state.has_delta()}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--a2ui",
        action="store_true",
        help="print the full A2UI messages for each widget",
    )
    arguments = parser.parse_args()
    for line in walk(show_a2ui=arguments.a2ui):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
