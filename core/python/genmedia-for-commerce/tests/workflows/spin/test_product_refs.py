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

"""One item on white with a border: the 360 spin's non-shoe references."""

import io

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.workflows.spin import product_refs

WHITE = (255, 255, 255)


def photo(*boxes, size=(400, 200)) -> bytes:
    """A white photo with red boxes ``(x0, y0, x1, y1)`` as the objects."""
    arr = np.full((size[1], size[0], 3), WHITE, np.uint8)
    for x0, y0, x1, y1 in boxes:
        arr[y0:y1, x0:x1] = (200, 30, 30)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


BLUE = (30, 30, 200)  # a person's sweater: foreground, but not the product


def worn_photo() -> bytes:
    """A red product (40..90 x 80..170) under a blue sweater it touches."""
    arr = np.full((200, 400, 3), WHITE, np.uint8)
    arr[20:80, 30:100] = BLUE
    arr[80:170, 40:90] = (200, 30, 30)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def busy_photo() -> bytes:
    """A red product (40..90 x 80..170) on random noise."""
    arr = np.random.default_rng(0).integers(0, 256, (200, 400, 3), np.uint8)
    arr[80:170, 40:90] = (200, 30, 30)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def outline(x0, y0, x1, y1, size=(400, 200)) -> dict:
    """The judge model's outline of the box (x0, y0, x1, y1), 0-1000."""
    nx, ny = (
        (lambda x: round(x * 1000 / size[0])),
        (lambda y: round(y * 1000 / size[1])),
    )
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return {
        "box_2d": [ny(y0), nx(x0), ny(y1), nx(x1)],
        "mask": [[nx(x), ny(y)] for x, y in corners],
        "label": "product",
    }


def blue_pixels(image: bytes) -> int:
    arr = np.array(Image.open(io.BytesIO(image)).convert("RGB")).astype(int)
    return int(((arr[..., 2] > 150) & (arr[..., 0] < 100)).sum())


def red_boxes(image: bytes) -> list[tuple[int, ...]]:
    """Bounding boxes of the red objects in ``image``, left to right."""
    import cv2

    arr = np.array(Image.open(io.BytesIO(image)).convert("RGB"))
    red = ((arr[..., 0] > 150) & (arr[..., 1] < 100)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(red)
    boxes = [tuple(int(v) for v in stats[i, :4]) for i in range(1, n)]
    return sorted(boxes)


def test_a_pair_is_cut_apart_and_the_largest_item_kept():
    # Two earrings far apart; the right one slightly larger.
    out = product_refs.one_item(photo((40, 40, 90, 160), (250, 30, 302, 170)))

    ((x, y, w, h),) = red_boxes(out)
    assert (w, h) == (52, 140)
    # A 10% border on every side of the kept item.
    assert Image.open(io.BytesIO(out)).size == (52 + 2 * 5, 140 + 2 * 14)
    assert (x, y) == (5, 14)


def test_a_small_detached_part_stays_with_its_item():
    # Left item with its hook drawn apart above it; a second item far right.
    out = product_refs.one_item(
        photo((60, 20, 66, 40), (40, 50, 90, 170), (250, 50, 300, 170))
    )

    boxes = red_boxes(out)
    assert len(boxes) == 2  # the item and its hook, not the other item
    assert Image.open(io.BytesIO(out)).size == (50 + 2 * 5, 150 + 2 * 15)


def test_touching_items_stay_whole():
    out = product_refs.one_item(photo((40, 40, 120, 160), (120, 60, 200, 150)))

    ((_, _, w, h),) = red_boxes(out)
    assert (w, h) == (160, 120)


def test_one_item_gets_the_border_and_nothing_is_cut():
    # Touches top and bottom; wide enough for a plain frame (a packshot).
    out = product_refs.one_item(photo((100, 0, 140, 200), size=(800, 200)))

    ((x, y, w, h),) = red_boxes(out)
    width, height = Image.open(io.BytesIO(out)).size
    assert (w, h) == (40, 200)
    assert (x, y, width - x - w, height - y - h) == (4, 20, 4, 20)


def test_no_foreground_returns_the_photo_unchanged():
    image = photo()
    assert product_refs.one_item(image) is image


def test_a_cut_by_name_leaves_the_person_behind(fake_gen):
    fake_gen.json({"boxes": [outline(40, 80, 90, 170)]})
    out = product_refs.one_item(worn_photo(), "red trousers")

    assert blue_pixels(out) == 0
    ((_, _, w, h),) = red_boxes(out)
    assert (w, h) == (50, 90)
    [call] = fake_gen.calls
    assert call["raw_parts"][0] == product_refs._CUT_ASK.format(
        name="red trousers"
    )


def test_a_packshot_on_a_plain_background_needs_no_model(fake_gen):
    out = product_refs.one_item(worn_photo())

    assert blue_pixels(out) > 0  # the whole foreground, as it is
    assert out != worn_photo()  # cropped with its border
    assert fake_gen.calls == []


def test_a_busy_background_photo_is_used_as_it_is(fake_gen):
    image = busy_photo()
    assert product_refs.one_item(image) is image
    assert fake_gen.calls == []  # no model


@pytest.mark.parametrize("failure", ["raises", "finds nothing"])
def test_a_failed_cut_by_name_uses_the_photo_as_it_is(fake_gen, failure):
    if failure == "raises":
        fake_gen.json(RuntimeError("judge down"))
    else:
        fake_gen.json({"boxes": []})
    image = worn_photo()

    assert product_refs.one_item(image, "red trousers") is image
    assert len(fake_gen.calls) == 1  # the cut by name only


def test_build_prefers_a_packshot_and_cuts_worn_photos_by_name(monkeypatch):
    seen = []
    monkeypatch.setattr(
        product_refs,
        "one_item",
        lambda image, name=None: seen.append((image, name)) or image,
    )
    images = [b"front-on-model", b"front-packshot", b"back-on-model"]
    views = [
        {"index": 0, "view": "front", "rank": 1, "presentation": "worn"},
        {"index": 1, "view": "front", "rank": 2, "presentation": "flat"},
        {"index": 2, "view": "back", "rank": 1, "presentation": "worn"},
    ]

    refs = product_refs.build(images, views, "grey trousers")

    # The packshot wins the front despite its worse rank; the back has only
    # a photo on a model, cut out by name.
    assert refs == {"front": b"front-packshot", "back": b"back-on-model"}
    assert sorted(seen) == [
        (b"back-on-model", "grey trousers"),
        (b"front-packshot", None),
    ]


def _views(*angles):
    return [
        {"index": i, "view": v, "rank": 1, "presentation": "flat"}
        for i, v in enumerate(angles)
    ]


@pytest.fixture
def no_cutout(monkeypatch):
    monkeypatch.setattr(
        product_refs, "one_item", lambda image, name=None: image
    )


def test_four_three_quarter_photos_are_all_kept_from_the_most_frontal(
    no_cutout,
):
    # The car whose photos are all three-quarter views: before, two of the
    # four (both rear ones) were dropped as a second "right" and "left".
    images = [b"front-right", b"front-left", b"back-left", b"back-right"]
    views = _views("front_right", "front_left", "back_left", "back_right")

    refs = product_refs.build(images, views, "a black SUV")

    assert list(refs) == [
        "front_right",
        "back_right",
        "back_left",
        "front_left",
    ]
    assert sorted(refs.values()) == sorted(images)


def test_a_straight_front_comes_first_then_around_the_circle(no_cutout):
    images = [b"left", b"back", b"front", b"front-right"]
    refs = product_refs.build(
        images, _views("left", "back", "front", "front_right"), "a mug"
    )
    assert list(refs) == ["front", "front_right", "back", "left"]


GREY = (230, 230, 230)


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_a_packshot_on_a_plain_background_keeps_its_own_pixels():
    # A red bag on grey: cropped from the photo itself, never cut out (a
    # cut-out bit two holes in the side photo of the leather handbag).
    arr = np.full((200, 400, 3), GREY, np.uint8)
    arr[40:160, 100:300] = (200, 30, 30)
    out = np.array(Image.open(io.BytesIO(product_refs.one_item(_png(arr)))))
    assert out.shape[:2] == (120 + 2 * 12, 200 + 2 * 20)  # the whole bag

    bag = out[14:130, 22:218].astype(int)  # inside the edges (JPEG)
    assert (np.abs(bag - (200, 30, 30)).sum(axis=2) < 30).all()  # its red
    assert (np.abs(out[:6].astype(int) - GREY).sum(axis=2) < 12).all()


def test_a_showroom_floor_along_the_border_is_used_as_it_is():
    # 90% of the border is white, the rest a grey floor: not a packshot.
    arr = np.full((200, 400, 3), WHITE, np.uint8)
    arr[170:, :] = (150, 150, 150)
    arr[60:160, 150:250] = (200, 30, 30)
    image = _png(arr)
    assert product_refs.one_item(image) is image
