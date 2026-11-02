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

"""``media.trim``: a photo cropped to its content on a plain background."""

import io

import numpy as np
from PIL import Image

from genmedia4commerce.core import media

WHITE = (255, 255, 255)


def encode(img: Image.Image, fmt: str = "PNG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def load(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def on_white(size, *boxes) -> Image.Image:
    """``size`` white canvas with each ``(box, colour)`` painted on it."""
    img = Image.new("RGB", size, WHITE)
    for box, colour in boxes:
        img.paste(colour, box)
    return img


def test_crops_to_the_content_with_a_4_percent_margin():
    out = load(
        media.trim(
            encode(on_white((1000, 1000), ((400, 300, 600, 700), (30, 30, 30))))
        )
    )
    assert out.format == "PNG"
    assert out.size == (200 + 32, 400 + 32)  # margin: 4% of 400 px


def test_a_thin_line_counts_at_full_resolution():
    # A 2 px chain above the pendant would vanish in a thumbnail.
    img = on_white(
        (4000, 4000),
        ((1800, 1800, 2200, 2200), (30, 30, 30)),
        ((2000, 400, 2002, 1800), (120, 120, 120)),
    )
    out = load(media.trim(encode(img)))
    assert out.size == (400 + 144, 1800 + 144)


def test_a_pale_product_on_white_is_kept_whole():
    img = on_white((1000, 1000), ((200, 200, 800, 800), (238, 238, 238)))
    assert load(media.trim(encode(img))).size == (600 + 48, 600 + 48)


def test_a_busy_border_is_left_alone():
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (300, 300, 3), dtype=np.uint8)
    data = encode(Image.fromarray(noise))
    assert media.trim(data) is data


def test_a_plain_photo_is_left_alone():
    data = encode(Image.new("RGB", (300, 300), WHITE))
    assert media.trim(data) is data


def test_a_person_cut_by_two_edges_is_still_cropped():
    # The body crosses the top and bottom edges: 20% of the border is not
    # background, the rest is still plain (60% rule).
    img = on_white((1000, 1000), ((300, 0, 700, 1000), (60, 60, 60)))
    assert load(media.trim(encode(img))).size == (400 + 80, 1000)


def test_a_transparent_png_stays_transparent():
    img = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    img.paste((200, 10, 10, 255), (100, 100, 300, 400))
    out = load(media.trim(encode(img)))
    assert out.mode == "RGBA"
    assert out.size == (200 + 24, 300 + 24)
    assert out.getpixel((0, 0))[3] == 0


def test_trimming_twice_changes_nothing():
    once = media.trim(
        encode(on_white((1000, 1000), ((400, 300, 600, 700), (30, 30, 30))))
    )
    assert media.trim(once) is once


def test_the_exif_rotation_is_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # shown rotated 90 degrees
    img = on_white((1000, 600), ((100, 100, 300, 200), (30, 30, 30)))
    data = encode(img, "JPEG", quality=100, subsampling=0, exif=exif)
    w, h = load(media.trim(data)).size
    assert abs(w - (100 + 16)) <= 4 and abs(h - (200 + 16)) <= 4


def test_a_light_gradient_background_is_cropped():
    # A grey studio backdrop graded by 28 levels: the tolerance follows the
    # border's spread, so the backdrop is background and the shoe is boxed.
    ramp = np.linspace(222, 250, 1000).astype(np.uint8)
    arr = np.repeat(np.tile(ramp, (1000, 1))[:, :, None], 3, axis=2)
    arr[400:600, 300:700] = 30
    out = load(media.trim(encode(Image.fromarray(arr))))
    assert out.size == (400 + 32, 200 + 32)


def test_crop_to_content_and_content_mask_share_the_rule():
    img = on_white((1000, 1000), ((400, 300, 600, 700), (30, 30, 30)))
    assert media.crop_to_content(img).size == (200 + 32, 400 + 32)
    mask, colour = media.content_mask(img)
    assert colour == WHITE and mask.sum() == 200 * 400
    busy = Image.fromarray(
        np.random.default_rng(0).integers(0, 255, (80, 80, 3), np.uint8)
    )
    assert media.content_mask(busy) is None
    assert media.crop_to_content(busy) is busy


def test_lone_specks_are_not_content():
    img = on_white((1000, 1000), ((400, 300, 600, 700), (30, 30, 30)))
    img.paste((0, 0, 0), (20, 20, 22, 22))  # a dust speck near the corner
    assert media.crop_to_content(img).size == (200 + 32, 400 + 32)
