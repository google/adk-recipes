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
"""Product thumbnails, generated rather than hosted.

The demo catalog is fictional, so there are no product photographs to link
to. Rather than ship dead ``https://example.com`` URLs that render as broken
images, each product gets a generated monogram tile inlined as a ``data:``
URI -- the same trick the spend chart uses.

That keeps the recipe genuinely self-contained: it runs with no network, no
asset bucket, and no placeholder service, and a reviewer sees real cards on
first run. Swap this module for real image URLs and nothing else changes.

Colour comes from a stable hash of the product id over a fixed palette, so a
product looks the same on every machine and in every test run. ``hash()``
would not do: Python salts string hashing per process.
"""

from __future__ import annotations

import hashlib
from xml.sax.saxutils import escape

from .chart_svg import svg_data_uri

_SIZE = 320

# Muted tones sharing roughly one value, so a row of cards reads as a set
# instead of a colour wheel. The first is the teal the spend chart uses.
_PALETTE = (
    "#2E7D6F",
    "#3F6C8E",
    "#8A6A4F",
    "#6B7F52",
    "#7A5C74",
    "#4E5A6B",
)

_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "Helvetica, Arial, sans-serif"
)


def tile_color(key: str) -> str:
    """A stable palette colour for a key.

    SHA-256 rather than ``hash()``: string hashing is salted per process, so
    ``hash()`` would give a product a different colour on every run and make
    the output untestable.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def monogram(name: str) -> str:
    """Up to two initials from a product name."""
    words = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    initials = "".join(w[0] for w in words[:2])
    return initials.upper() or "?"


def product_tile_svg(product_id: str, name: str) -> str:
    """A square monogram tile for one product."""
    color = tile_color(product_id)
    label = escape(monogram(name))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SIZE}"'
        f' height="{_SIZE}" viewBox="0 0 {_SIZE} {_SIZE}" role="img">'
        f'<rect width="{_SIZE}" height="{_SIZE}" fill="{color}"/>'
        # A soft off-centre disc for depth, clipped by the tile edge.
        f'<circle cx="{_SIZE}" cy="{_SIZE}" r="{_SIZE * 0.62:.0f}"'
        ' fill="#FFFFFF" fill-opacity="0.10"/>'
        f'<circle cx="0" cy="0" r="{_SIZE * 0.38:.0f}"'
        ' fill="#000000" fill-opacity="0.07"/>'
        f'<text x="{_SIZE / 2:.0f}" y="{_SIZE / 2:.0f}"'
        ' text-anchor="middle" dominant-baseline="central"'
        f' font-family="{_FONT}" font-size="{_SIZE * 0.34:.0f}"'
        ' font-weight="300" letter-spacing="2"'
        ' fill="#FFFFFF" fill-opacity="0.92">'
        f"{label}</text>"
        "</svg>"
    )


def product_tile_uri(product_id: str, name: str) -> str:
    """The monogram tile as a ``data:`` URI, ready for ``Image.url``."""
    return svg_data_uri(product_tile_svg(product_id, name))
