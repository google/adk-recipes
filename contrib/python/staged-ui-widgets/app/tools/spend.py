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
"""Spend history, as a chart.

This is the plainest argument in the recipe for deterministic rendering. A
language model cannot plot a series. Asked for SVG it will produce path data
that looks like a chart and encodes the wrong numbers, and nothing downstream
can tell. The geometry is computed in ``render/chart_svg.py`` from the same
list of amounts the summary is computed from, so the headline and the line
cannot disagree.

The note under the chart is computed here for the same reason: an average
stated by the model is an average nobody checked.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from .. import store
from ..render.converters import money
from ..staging import stage_widget

_MONTH_LABELS = (
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

_DEFAULT_WINDOW = 6
_MAX_WINDOW = 12


def get_spend_summary(
    months: int,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Shows the shopper's spending over recent months as a chart.

    Args:
      months: How many recent months to chart, 1 to 12. Pass 0 for the
        default six.

    Returns:
      The totals behind the chart -- window, sum, average, and the biggest
      month -- so you can answer follow-up questions without re-reading the
      chart. The shopper sees the chart itself, so lead with the trend rather
      than reciting every month.
    """
    window = _window(months)
    history = store.spend_months()
    if not history:
        return {
            "status": "empty",
            "widget": None,
            "summary": "No spending history on this account.",
        }

    recent = history[-window:]
    points = [
        {
            "month": _label(entry.get("month")),
            "amount": entry.get("amount", 0.0),
        }
        for entry in recent
    ]
    amounts = [
        float(p["amount"])
        for p in points
        if isinstance(p["amount"], (int, float))
    ]
    if not amounts:
        return {
            "status": "empty",
            "widget": None,
            "summary": "The spending history has no usable amounts.",
        }

    total = sum(amounts)
    average = total / len(amounts)
    peak = max(recent, key=lambda e: e.get("amount", 0.0))
    top_category, top_amount = _top_category(recent)

    stage_widget(
        tool_context.state,
        "spend",
        {
            "headline": f"Spending, last {len(points)} months",
            "points": points,
            "note": f"Averaging {money(round(average, 2))} a month",
        },
    )

    return {
        "status": "ok",
        "widget": "spend",
        "months": len(points),
        "total": round(total, 2),
        "average": round(average, 2),
        "highest_month": _label(peak.get("month")),
        "highest_amount": peak.get("amount"),
        "top_category": top_category,
        "top_category_amount": top_amount,
        "summary": (
            f"Staged a {len(points)}-month spend chart. Total "
            f"{money(round(total, 2))}, average "
            f"{money(round(average, 2))} a month, highest in "
            f"{_label(peak.get('month'))}."
        ),
    }


def _window(months: int) -> int:
    """Clamps the requested window to something the data supports.

    ``0`` means "you decide", which is what a model passes when the shopper
    did not say. Out-of-range values are clamped rather than rejected: the
    shopper asked about spending, and an error message is a worse answer than
    twelve months.
    """
    if not isinstance(months, int) or months <= 0:
        return _DEFAULT_WINDOW
    return min(months, _MAX_WINDOW)


def _label(month: Any) -> str:
    """``2026-08`` -> ``Aug``, falling back to the raw value.

    Month names alone are enough for a chart spanning under a year, which is
    the only window this tool offers.
    """
    text = str(month or "")
    parts = text.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        index = int(parts[1])
        if 1 <= index <= 12:
            return _MONTH_LABELS[index - 1]
    return text


def _top_category(
    entries: list[dict[str, Any]],
) -> tuple[str | None, float | None]:
    """The biggest spending category across the window.

    Ties break on the category name so the same window always names the same
    category.
    """
    totals: dict[str, float] = {}
    for entry in entries:
        breakdown = entry.get("by_category") or {}
        if not isinstance(breakdown, dict):
            continue
        for category, amount in breakdown.items():
            if isinstance(amount, (int, float)):
                totals[category] = totals.get(category, 0.0) + float(amount)
    if not totals:
        return None, None
    best = min(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return best[0], round(best[1], 2)
