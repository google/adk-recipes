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
"""Server-rendered spend chart, delivered as a ``data:`` URI.

The A2UI ``Image`` component takes any URL, including a ``data:`` URI, so a
chart can be computed in Python and inlined into the surface with no asset
host, no CDN, and no client-side charting library.

This is the clearest case for deterministic rendering in the whole recipe: a
language model cannot plot a series, and asking one to emit SVG path data
produces plausible-looking but wrong geometry.

Geometry is rounded to two decimals so the output is byte-stable and can be
asserted in tests.
"""

from __future__ import annotations

import base64
from typing import Any
from xml.sax.saxutils import escape

# Chart box, in SVG user units.
_WIDTH = 480
_HEIGHT = 180

# Room for the value label above and month labels below.
_PAD_LEFT = 10
_PAD_RIGHT = 10
_PAD_TOP = 22
_PAD_BOTTOM = 26

# Mid-tone ink chosen to stay legible on either a light or a dark client
# ground, since an inlined image cannot inherit the surface theme.
_LINE = "#2E7D6F"
_FILL = "#2E7D6F"
_LABEL = "#6B7280"
_BASELINE = "#9CA3AF"

_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif"
)


def _num(value: float) -> str:
    """Formats a coordinate stably, without a trailing ``.0``."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def spend_trend_svg(points: list[dict[str, Any]]) -> str:
    """Renders ``[{"month": "Jan", "amount": 120.0}, ...]`` as SVG markup.

    Returns an empty string for an empty series, which the converter treats
    as "nothing to render" rather than emitting a blank chart.
    """
    series = [p for p in points if isinstance(p.get("amount"), (int, float))]
    if not series:
        return ""

    amounts = [float(p["amount"]) for p in series]
    peak = max(amounts)
    # Headroom above the peak so the high point is not flush with the top,
    # and a floor so an all-zero series still has a sane scale.
    scale_max = peak * 1.15 if peak > 0 else 1.0

    plot_w = _WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_h = _HEIGHT - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _PAD_TOP + plot_h

    if len(series) == 1:
        xs = [_PAD_LEFT + plot_w / 2]
    else:
        step = plot_w / (len(series) - 1)
        xs = [_PAD_LEFT + i * step for i in range(len(series))]
    ys = [baseline_y - (a / scale_max) * plot_h for a in amounts]

    line_pts = " ".join(
        f"{_num(x)},{_num(y)}" for x, y in zip(xs, ys, strict=True)
    )
    area_pts = (
        f"{_num(xs[0])},{_num(baseline_y)} "
        f"{line_pts} "
        f"{_num(xs[-1])},{_num(baseline_y)}"
    )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}"'
        f' height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}"'
        ' role="img">',
        f'<polygon points="{area_pts}" fill="{_FILL}" fill-opacity="0.14"/>',
        f'<polyline points="{line_pts}" fill="none" stroke="{_LINE}"'
        ' stroke-width="2" stroke-linejoin="round"'
        ' stroke-linecap="round"/>',
        f'<line x1="{_num(_PAD_LEFT)}" y1="{_num(baseline_y)}"'
        f' x2="{_num(_WIDTH - _PAD_RIGHT)}" y2="{_num(baseline_y)}"'
        f' stroke="{_BASELINE}" stroke-width="1"/>',
    ]

    # Emphasise the most recent point -- the one the reader came for.
    parts.append(
        f'<circle cx="{_num(xs[-1])}" cy="{_num(ys[-1])}" r="3.5"'
        f' fill="{_LINE}"/>'
    )
    parts.append(
        f'<text x="{_num(xs[-1])}" y="{_num(ys[-1] - 9)}"'
        f' text-anchor="middle" font-family="{_FONT}" font-size="12"'
        f' font-weight="600" fill="{_LINE}">'
        f"{escape(_money(amounts[-1]))}</text>"
    )

    for x, point in zip(xs, series, strict=True):
        label = escape(str(point.get("month", "")))
        parts.append(
            f'<text x="{_num(x)}" y="{_num(_HEIGHT - 8)}"'
            f' text-anchor="middle" font-family="{_FONT}" font-size="11"'
            f' fill="{_LABEL}">{label}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _money(amount: float) -> str:
    """Whole-dollar formatting, matching the converters' price style."""
    return f"${amount:,.0f}"


def svg_data_uri(svg: str) -> str:
    """Wraps SVG markup as a base64 ``data:`` URI.

    Base64 rather than percent-encoding so the payload survives clients that
    are careless about quoting inside a URL.
    """
    if not svg:
        return ""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
