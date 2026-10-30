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

"""Tests for the product cards of an outfit's front drawing (layout only)."""

import io

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows.tryon import cards

PRODUCT = (40, 90, 160)
CROPS = [(200, 40, 40), (40, 160, 60), (230, 180, 30)]


def solid(w: int, h: int, color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def pixels(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)


def box_of(arr: np.ndarray, color) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the pixels of exactly ``color``."""
    ys, xs = np.nonzero((arr == color).all(axis=2))
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def test_a_card_pastes_each_crop_at_its_own_size():
    product = solid(1000, 1500, PRODUCT)
    crops = [("A1: padlock", solid(300, 200, CROPS[0]))]
    arr = pixels(cards.card(product, crops))
    x0, y0, x1, y1 = box_of(arr, CROPS[0])
    assert (x1 - x0, y1 - y0) == (300, 200)
    assert (arr[y0:y1, x0:x1] == CROPS[0]).all()  # no text over it
    _, _, px1, _ = box_of(arr, PRODUCT)
    assert px1 <= x0  # beside the tall product


def test_a_long_crop_is_capped_beside_a_tall_product_and_under_a_wide_one():
    beside = pixels(
        cards.card(
            solid(2000, 3000, PRODUCT),
            [("A1: plaque", solid(1200, 400, CROPS[0]))],
        )
    )
    x0, y0, x1, y1 = box_of(beside, CROPS[0])
    assert (x1 - x0, y1 - y0) == (900, 300)
    under = pixels(
        cards.card(
            solid(3000, 1500, PRODUCT),
            [("A1: strap", solid(1000, 400, CROPS[1]))],
        )
    )
    x0, y0, x1, y1 = box_of(under, CROPS[1])
    assert (x1 - x0, y1 - y0) == (700, 280)
    _, _, _, py1 = box_of(under, PRODUCT)
    assert py1 <= y0  # under the wide product


def test_a_label_sits_on_a_white_band_above_its_crop():
    label = "A1: MAISON ORVELLE label"
    arr = pixels(
        cards.card(
            solid(1000, 1500, PRODUCT), [(label, solid(300, 200, CROPS[0]))]
        )
    )
    x0, y0, x1, _ = box_of(arr, CROPS[0])
    band = arr[y0 - cards.band_height(label, 300) : y0, x0:x1]
    ink = (band < 128).all(axis=2)
    assert ink.any()  # the text is drawn
    white = (band == 255).all(axis=2)
    grey = (band.max(axis=2) - band.min(axis=2)) < 8  # anti-aliased ink
    assert (white | ink | grey).all()  # nothing but white and text


def test_a_long_label_wraps_inside_the_crop_width():
    label = "A1: MAISON ORVELLE PARIS embossed stamp near the bottom edge"
    assert cards.band_height(label, 120) > cards.band_height(label, 900)


def test_a_card_over_2_mp_shrinks_as_a_whole():
    product = solid(2000, 3000, PRODUCT)
    crops = [(f"A{k}: detail", solid(900, 900, c)) for k, c in enumerate(CROPS)]
    data = cards.card(product, crops)
    w, h = Image.open(io.BytesIO(data)).size
    assert w * h <= cards.CARD_MAX_PX
    assert w % 16 == 0 and h % 16 == 0


@pytest.mark.parametrize(
    ("size", "kept"),
    [((610, 100), False), ((600, 100), True), ((100, 910), False)],
)
def test_a_crop_wider_or_taller_than_60_percent_is_dropped(size, kept):
    product = solid(1000, 1500, PRODUCT)
    crops = [("A1: pocket flap", solid(*size, CROPS[0]))]
    assert cards.useful(product, crops) == ([0] if kept else [])


def test_a_crop_shown_smaller_than_the_product_shows_it_is_dropped():
    """The product is drawn at least 1100 px tall: a 400 px tall product
    grows, and a crop from it at its own size would be smaller than its
    area on the product."""
    small = solid(300, 400, PRODUCT)
    crops = [("A1: logo", solid(120, 120, CROPS[0]))]
    assert cards.useful(small, crops) == []
    big = solid(1000, 1500, PRODUCT)
    assert cards.useful(big, crops) == [0]


def test_useful_drops_the_smallest_zoom_first():
    """Three long crops (capped to 500x900) make a column so tall that the
    product is drawn larger than they are; they go one by one until the
    rest show their area larger than the product does."""
    product = solid(2000, 3000, PRODUCT)
    crops = [(f"A{k}: plaque", solid(1000, 1800, CROPS[k])) for k in range(3)]
    crops.append(("A4: snap", solid(200, 200, PRODUCT)))
    assert cards.useful(product, crops) == [2, 3]


@pytest.mark.parametrize("size", [(600, 1200), (300, 360), (1200, 600)])
def test_single_is_about_1_2_mp_with_no_text(size):
    arr = pixels(cards.single(solid(*size, PRODUCT)))
    h, w = arr.shape[:2]
    assert w % 16 == 0 and h % 16 == 0
    assert abs(w * h - cards.IMAGE_PX) < 0.05 * cards.IMAGE_PX
    assert (np.abs(arr - PRODUCT) <= 1).all()  # nothing drawn on it


def test_tight_cuts_plain_margins_and_flattens_transparency():
    canvas = solid(400, 400, (255, 255, 255))
    canvas.paste(solid(100, 200, PRODUCT), (150, 100))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    w, h = cards.tight(buf.getvalue()).size
    assert (w, h) == (104, 204)  # 1% of the long side kept, at least 2 px
    clear = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    buf = io.BytesIO()
    clear.save(buf, format="PNG")
    flat = cards.tight(buf.getvalue())
    assert flat.mode == "RGB" and flat.getpixel((0, 0)) == (255, 255, 255)
    blank = cards.tight(png(30, 20))  # nothing on the background
    assert blank.size == (30, 20)


def test_whole_keeps_the_canvas_upright_and_flattens_transparency():
    canvas = solid(400, 300, (255, 255, 255))
    canvas.paste(solid(50, 100, PRODUCT), (150, 100))
    exif = Image.Exif()
    exif[0x0112] = 6  # stored on its side: turned upright
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", exif=exif)
    img = cards.whole(buf.getvalue())
    assert (img.size, img.mode) == ((300, 400), "RGB")  # never cut tight
    clear = Image.new("RGBA", (50, 40), (0, 0, 0, 0))
    buf = io.BytesIO()
    clear.save(buf, format="PNG")
    flat = cards.whole(buf.getvalue())
    assert flat.size == (50, 40) and flat.getpixel((0, 0)) == (255, 255, 255)


def test_a_closures_button_row_stays_however_long():
    """Cut to a few buttons, a long vest came out a short waistcoat; the
    whole row shows the count, the spacing and where the garment ends."""
    product = solid(1000, 1400, PRODUCT)
    crops = [("B1: center front closure", solid(200, 1300, CROPS[0]))]
    assert cards.useful(product, crops, rows=[0]) == [0]
    assert cards.useful(product, crops) == []  # not a row: over 60%
