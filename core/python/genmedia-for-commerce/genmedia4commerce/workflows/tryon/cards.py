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

"""Product cards: the images of an outfit's front drawing (``outfit_prompt``).

Every product gets its own image, so its weave and print keep their pixels
(packed into one collage, each product got a fraction of the image). A
product with close-ups is a card (``card``): the product, with no text, and
its close-ups at their own size, each under a label, beside a tall product
or under a wide one. Everything else (a product without close-ups, the
wearer's body and face, a look-photo crop) is one image of about
``IMAGE_PX`` with no text (``single``). Every image but the wearer's body
is cropped to its content first (``tight``); the body keeps its canvas and
aspect ratio (``whole``). A crop that is not really a close-up is dropped
(``useful``). In an A/B test (4 runs of 10 outfits, 3 images each) cards
published 78 of 120 images against 71 for the collage with separate
close-ups, with fewer fabric and print rejections.

A label sits on a white band above its close-up, never over it, wrapped to
the close-up's width, in Pillow's built-in font (``ImageFont.load_default``,
no font file needed).
"""

import functools
import io
import math
import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from genmedia4commerce.core import media

__all__ = [
    "IMAGE_PX",
    "band_height",
    "card",
    "single",
    "tight",
    "useful",
    "whole",
]

IMAGE_PX = 960 * 1280  # ``single``: about 1.2 MP
MULTIPLE = 16  # the sides of every image are multiples of this
PAD = 16  # px around and between the images of a card
TRIM_MARGIN = 0.01  # ``tight`` keeps this share of the longer side
FONT_PX = 28  # label text
LINE_PX = round(FONT_PX * 1.25)
MARGIN = 6  # px of white above and below a label
INK = (17, 17, 17)
CROP_CAP_SIDE = 900  # a close-up's long side beside a tall product
CROP_CAP_ROW = 700  # under a wide product
WIDE = 1.3  # a product wider than this x its height has its close-ups under it
CARD_MIN_SIDE = 1100  # the product's side along its close-ups, at least
CARD_MAX_PX = 2_000_000  # a larger card shrinks as a whole
MAX_CROP_SHARE = 0.6  # a crop wider or taller than this share of its product
MIN_ZOOM = 1.0  # a close-up shows its area at least as big as the product

Crop = tuple[str, Image.Image]  # (label, close-up)


@functools.cache
def _font() -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=FONT_PX)


@functools.cache
def _has_glyph(ch: str) -> bool:
    """The font draws ``ch``, not its missing-glyph box."""

    def mask(c: str) -> bytes:
        im = Image.new("L", (2 * FONT_PX, 2 * FONT_PX), 0)
        ImageDraw.Draw(im).text((4, 4), c, fill=255, font=_font())
        return im.tobytes()

    return ch.isspace() or mask(ch) != mask("\uffff")


def _drawable(text: str) -> str:
    """``text`` in glyphs the font has: dashes as '-', accents dropped
    ('é' -> 'e'), anything else missing as '?'."""
    out = []
    for ch in text.replace("\u2014", "-").replace("\u2013", "-"):
        base = unicodedata.normalize("NFKD", ch)[:1]
        if _has_glyph(ch):
            out.append(ch)
        else:
            out.append(base if base and _has_glyph(base) else "?")
    return "".join(out)


def _wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap to ``width`` px; a longer word is split."""
    font = _font()

    def fits(s: str) -> bool:
        return font.getlength(s) <= width

    lines: list[str] = []
    for word in _drawable(text).split():
        rest = word
        while len(rest) > 1 and not fits(rest):
            cut = range(len(rest) - 1, 0, -1)
            k = next((i for i in cut if fits(rest[:i])), 1)
            lines.append(rest[:k])
            rest = rest[k:]
        if lines and fits(f"{lines[-1]} {rest}"):
            lines[-1] += f" {rest}"
        else:
            lines.append(rest)
    return lines or [""]


def band_height(text: str, width: int) -> int:
    """The height of the white band of a label ``width`` px wide."""
    return 2 * MARGIN + len(_wrap(text, width)) * LINE_PX


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def tight(data: bytes) -> Image.Image:
    """The image cropped to its content on a plain background (the margins
    cut, ``TRIM_MARGIN`` kept), RGB on white; as it is on a busy one."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    found = media.content_mask(img)
    if found is not None and found[0].any():
        ys, xs = np.nonzero(found[0])
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        m = max(2, round(TRIM_MARGIN * max(x1 - x0, y1 - y0)))
        img = img.crop(
            (
                max(0, x0 - m),
                max(0, y0 - m),
                min(img.width, x1 + m),
                min(img.height, y1 + m),
            )
        )
    return _on_white(img)


def whole(data: bytes) -> Image.Image:
    """The image as it is (no crop: its canvas and aspect ratio), upright
    (EXIF), RGB on white."""
    return _on_white(ImageOps.exif_transpose(Image.open(io.BytesIO(data))))


def _on_white(img: Image.Image) -> Image.Image:
    """``img`` in RGB, any transparency on white."""
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        white = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(white, img.convert("RGBA"))
    return img.convert("RGB")


def _border(img: Image.Image) -> tuple[int, ...]:
    """The median colour of the image's 1-pixel border."""
    a = np.asarray(img)
    edge = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return tuple(int(v) for v in np.median(edge, axis=0))


def single(img: Image.Image, target: int = IMAGE_PX) -> bytes:
    """``img`` resized to about ``target`` px, sides multiples of
    ``MULTIPLE`` (the few px of rounding filled with its own border
    colour); no text."""
    w, h = img.size
    s = math.sqrt(target / (w * h))
    box = tuple(
        max(MULTIPLE, round(v * s / MULTIPLE) * MULTIPLE) for v in (w, h)
    )
    k = min(box[0] / w, box[1] / h)
    size = (
        min(box[0], max(1, round(w * k))),
        min(box[1], max(1, round(h * k))),
    )
    resized = img.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", box, _border(resized))
    canvas.paste(resized, ((box[0] - size[0]) // 2, (box[1] - size[1]) // 2))
    return _png(canvas)


@dataclass
class _Tile:
    """``source`` drawn at ``w`` x ``h`` under its label's band (no label:
    no band)."""

    label: str
    source: Image.Image
    w: int
    h: int

    @functools.cached_property
    def band(self) -> int:
        return band_height(self.label, self.w) if self.label else 0

    @property
    def height(self) -> int:
        return self.band + self.h


def _scaled(label: str, img: Image.Image, s: float) -> _Tile:
    w, h = (max(1, round(v * s)) for v in img.size)
    return _Tile(label, img, w, h)


Placed = list[tuple[_Tile, int, int]]  # each tile with its top-left corner


def _place(
    product: Image.Image, crops: list[Crop]
) -> tuple[int, int, Placed, list[float]]:
    """(width, height, placed tiles, zoom of each crop) of a card: the
    close-ups at their own size (long side capped at ``CROP_CAP_SIDE``, or
    ``CROP_CAP_ROW`` in a row) beside a tall product, as tall as their
    column and at least ``CARD_MIN_SIDE``, or under a wide one, as wide as
    their row and at least ``CARD_MIN_SIDE``. A crop's zoom: how much bigger
    it shows its area than the product image does."""
    wide = product.width > WIDE * product.height
    cap = CROP_CAP_ROW if wide else CROP_CAP_SIDE
    tiles = [
        _scaled(label, img, min(1.0, cap / max(img.size)))
        for label, img in crops
    ]
    if wide:
        row = sum(t.w for t in tiles) + PAD * (len(tiles) - 1)
        pw = max(row, CARD_MIN_SIDE)
        ph = max(1, round(pw * product.height / product.width))
        placed = [(_Tile("", product, pw, ph), PAD, PAD)]
        x = PAD
        for t in tiles:
            placed.append((t, x, 2 * PAD + ph))
            x += t.w + PAD
        width = 2 * PAD + max(pw, row)
        height = 3 * PAD + ph + max(t.height for t in tiles)
    else:
        column = sum(t.height for t in tiles) + PAD * (len(tiles) - 1)
        ph = max(column, CARD_MIN_SIDE)
        pw = max(1, round(ph * product.width / product.height))
        placed = [(_Tile("", product, pw, ph), PAD, PAD)]
        y = PAD
        for t in tiles:
            placed.append((t, 2 * PAD + pw, y))
            y += t.height + PAD
        width = 3 * PAD + pw + max(t.w for t in tiles)
        height = 2 * PAD + max(ph, column)
    scale = pw / product.width
    return width, height, placed, [t.w / t.source.width / scale for t in tiles]


def _render(width: int, height: int, placed: Placed) -> Image.Image:
    """Each tile (its label's band, then its image) at its corner on a
    white canvas grown to sides multiples of ``MULTIPLE``, content
    centred."""
    full = tuple(math.ceil(v / MULTIPLE) * MULTIPLE for v in (width, height))
    dx, dy = (full[0] - width) // 2, (full[1] - height) // 2
    canvas = Image.new("RGB", full, "white")
    draw = ImageDraw.Draw(canvas)
    for tile, x, y in placed:
        left, top = x + dx, y + dy
        lines = _wrap(tile.label, tile.w) if tile.label else []
        for k, line in enumerate(lines):
            at = (left, top + MARGIN + k * LINE_PX)
            draw.text(at, line, fill=INK, font=_font())
        image = tile.source
        if image.size != (tile.w, tile.h):
            image = image.resize((tile.w, tile.h), Image.Resampling.LANCZOS)
        canvas.paste(image, (left, top + tile.band))
    return canvas


# A closure's row: buttons, snaps, studs or rivets (from what the detail
# search said it is).
ROW = re.compile(r"button|snap|stud|rivet|placket", re.I)


def useful(
    product: Image.Image, crops: list[Crop], rows: Collection[int] = ()
) -> list[int]:
    """Indexes of the crops a card keeps. A closure's row of buttons, snaps
    or rivets always stays: it shows their count, spacing and where the
    garment ends (cut to a few, a long vest came out a short waistcoat). Any
    other crop wider or taller than ``MAX_CROP_SHARE`` of its product is not
    a close-up (a box over most of the garment), and a kept one never shows
    its area smaller than the product does (the smallest zoom goes first).
    ``rows``: the indexes of the closure rows."""
    rows = set(rows)
    keep = [
        k
        for k, (_, img) in enumerate(crops)
        if k in rows
        or (
            img.width <= MAX_CROP_SHARE * product.width
            and img.height <= MAX_CROP_SHARE * product.height
        )
    ]
    while True:
        judged = [k for k in keep if k not in rows]
        if not judged:
            return keep
        *_, zooms = _place(product, [crops[k] for k in keep])
        zoom = dict(zip(keep, zooms, strict=True))
        worst = min(judged, key=zoom.__getitem__)
        if zoom[worst] >= MIN_ZOOM:
            return keep
        keep.remove(worst)


def card(product: Image.Image, crops: list[Crop]) -> bytes:
    """The product, with no label, and its labelled close-ups (``_place``);
    a card over ``CARD_MAX_PX`` shrinks as a whole."""
    img = _render(*_place(product, crops)[:3])
    pixels = img.width * img.height
    if pixels > CARD_MAX_PX:
        k = math.sqrt(CARD_MAX_PX / pixels)
        size = tuple(
            max(MULTIPLE, int(v * k) // MULTIPLE * MULTIPLE) for v in img.size
        )
        img = img.resize(size, Image.Resampling.LANCZOS)
    return _png(img)
