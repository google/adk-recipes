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
"""Order status, as a timeline the shopper can read at a glance.

The fixtures store facts -- a date, whether the parcel reached a stage -- and
never a display state. ``_step_state`` derives done / current / upcoming from
position, which is the small computation that makes the timeline impossible
to contradict: there is exactly one current step because only one stage can
be the last reached one.

Two states cannot be derived from position, so a stage may pin them
explicitly: a problem that needs the shopper's attention, and a cancellation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from google.adk.tools import ToolContext

from .. import store
from ..staging import stage_widget

_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# States a stage may declare for itself, because position cannot express them.
_PINNED_STATES = {"problem", "cancelled"}


def get_order_status(
    order_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Shows the delivery timeline for an order.

    Args:
      order_id: The order id, e.g. "ORD-4417". Pass an empty string for the
        shopper's most recent order that is still in progress.

    Returns:
      The order's current stage and what it contains. The shopper sees the
      full timeline as a widget, so summarise rather than narrating each step.
    """
    cleaned = order_id.strip()
    if cleaned:
        found = store.order(cleaned)
        if found is None:
            known = ", ".join(o["id"] for o in store.orders())
            return {
                "status": "not_found",
                "widget": None,
                "summary": (
                    f"No order {cleaned!r} on this account. "
                    f"Known orders: {known}."
                ),
            }
    else:
        found = store.latest_open_order()
        if found is None:
            return {
                "status": "empty",
                "widget": None,
                "summary": "Every order on this account has been completed.",
            }

    steps = _steps(found)
    stage_widget(
        tool_context.state,
        "order",
        {"headline": f"Order {found['id']}", "steps": steps},
    )

    # Pinned states are "where the order stands" too. A cancelled order has no
    # step in the ``current`` position, and falling through to the finished-
    # order default below would tell the model the parcel arrived -- the
    # opposite of what the timeline beside it shows.
    current = next(
        (s for s in steps if s["state"] in ("current", *_PINNED_STATES)),
        None,
    )
    # Nothing in progress means the order ran to its end, so the last stage is
    # where it stands. Read from the data rather than assuming "Delivered".
    standing = current["label"] if current else _last_label(steps)
    item_names = [
        product["name"]
        for product in (store.product(i) for i in found.get("item_ids", []))
        if product
    ]

    return {
        "status": "ok",
        "widget": "order",
        "order_id": found["id"],
        "current_step": standing,
        # "problem" only: a cancelled order is settled, so there is nothing for
        # the shopper to do about it. ``current_step`` is what says so.
        "needs_attention": bool(current and current["state"] == "problem"),
        "eta": found.get("eta"),
        "items": item_names,
        "summary": (
            f"Staged the timeline for {found['id']}. Currently: {standing}."
        ),
    }


def _last_label(steps: list[dict[str, Any]]) -> str:
    """The final stage's label, for an order with nothing left in progress.

    An order with no stages at all has no answer in the data, so say that
    rather than naming a stage it was never recorded as reaching -- naming one
    is the assumption this function exists to remove.
    """
    return steps[-1]["label"] if steps else "Unknown"


def _steps(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turns stored stages into timeline steps with derived states."""
    stages = [s for s in order.get("stages", []) if isinstance(s, Mapping)]
    last_reached = _last_reached_index(stages)
    has_unreached = any(not s.get("reached") for s in stages)

    steps = []
    eta_used = False
    for index, stage in enumerate(stages):
        state = _step_state(stage, index, last_reached, has_unreached)
        # Only the first upcoming step carries the ETA; repeating it down the
        # list would imply every remaining stage lands on the same day.
        carries_eta = state == "upcoming" and not eta_used
        if carries_eta:
            eta_used = True
        steps.append(
            {
                "label": str(stage.get("label", "")),
                "state": state,
                "detail": _detail(stage, state, order, carries_eta),
            }
        )
    return steps


def _last_reached_index(stages: list[Mapping[str, Any]]) -> int:
    last = -1
    for index, stage in enumerate(stages):
        if stage.get("reached"):
            last = index
    return last


def _step_state(
    stage: Mapping[str, Any],
    index: int,
    last_reached: int,
    has_unreached: bool,
) -> str:
    """One stage's display state.

    A pinned state always wins. Otherwise: reached stages are done, except
    the furthest one, which is *current* only while something is still
    outstanding -- once the last stage is reached nothing is in progress and
    every step reads as done.
    """
    pinned = str(stage.get("state", "")).lower()
    if pinned in _PINNED_STATES:
        return pinned
    if not stage.get("reached"):
        return "upcoming"
    if index == last_reached and has_unreached:
        return "current"
    return "done"


def _detail(
    stage: Mapping[str, Any],
    state: str,
    order: Mapping[str, Any],
    carries_eta: bool,
) -> str:
    """The line under a step label: when it happened, or when it will.

    Upcoming steps have no date of their own, so the first one carries the
    order's ETA -- which is the number the shopper actually opened the
    timeline to find.
    """
    note = str(stage.get("note", "")).strip()
    parts: list[str] = []

    if state == "upcoming":
        eta = _format_date(order.get("eta")) if carries_eta else ""
        if eta:
            parts.append(f"Expected {eta}")
    else:
        when = _format_date(stage.get("on"))
        if when:
            parts.append(when)

    if note:
        parts.append(note)
    return " · ".join(parts)


def _format_date(value: Any) -> str:
    """``2026-08-12`` -> ``Aug 12``. Returns ``""`` for anything unparsable."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return ""
    return f"{_MONTHS[parsed.month - 1]} {parsed.day}"
