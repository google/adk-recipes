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

import io

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core.models import get_models
from genmedia4commerce.workflows.spin import shoe_split as split
from genmedia4commerce.workflows.spin.shoe_prompts import MASK_ASK, MASK_SCHEMA

BG, SHOE = 230, 40


def _rect(shape, x0, y0, x1, y1) -> np.ndarray:
    mask = np.zeros(shape, bool)
    mask[y0:y1, x0:x1] = True
    return mask


def _scene(shape, *masks) -> np.ndarray:
    """A plain light background with each mask painted dark."""
    arr = np.full((*shape, 3), BG, np.uint8)
    for mask in masks:
        arr[mask] = SHOE
    return arr


# ---------------------------------------------------------------- decode


def test_decode_reads_swapped_points_with_the_reading_that_fits_the_box():
    # A wide shoe at x 100-400, y 50-150 of a 500 x 250 photo; the points
    # come as [y, x] (0-1000) instead of [x, y].
    item = {
        "box_2d": [200, 200, 600, 800],
        "mask": [[200, 200], [200, 800], [600, 800], [600, 200]],
    }
    poly, fit = split.decode(item, 500, 250)
    assert fit == pytest.approx(1.0)
    assert sorted(poly) == sorted(
        [(100.0, 50.0), (400.0, 50.0), (400.0, 150.0), (100.0, 150.0)]
    )


def test_decode_prefers_the_documented_reading_on_a_tie():
    square = [[100, 100], [100, 900], [900, 900], [900, 100]]  # x/y alike
    poly, fit = split.decode(
        {"box_2d": [100, 100, 900, 900], "mask": square}, 10, 10
    )
    assert fit == pytest.approx(1.0)  # xy/image: no box-relative penalty
    assert (1.0, 1.0) in poly


@pytest.mark.parametrize(
    "item",
    [
        {"mask": [[0, 0], [10, 0], [10, 10]]},  # no box
        {"box_2d": [0, 0, 10, 10], "mask": [[0, 0], [10, 0]]},  # 2 points
    ],
)
def test_decode_without_a_box_or_a_polygon_is_none(item):
    assert split.decode(item, 100, 100) == (None, 0.0)


def test_iou():
    assert split.iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert split.iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)
    assert split.iou((0, 0, 1, 1), (2, 2, 3, 3)) == 0.0


# ---------------------------------------------------------------- masks call


def test_ask_masks_sends_the_photo_at_1024_and_decodes_in_full_size(fake_gen):
    photo = io.BytesIO()
    Image.new("RGB", (2048, 1024), "white").save(photo, format="PNG")
    fake_gen.json(
        {
            "boxes": [
                {
                    "box_2d": [250, 100, 750, 400],
                    "mask": [[100, 250], [400, 250], [400, 750], [100, 750]],
                    "label": "shoe",
                    "complete": True,
                    "view": "front_right",
                },
                {"box_2d": [0, 0, 10, 10], "mask": [], "label": "x"},  # dropped
            ]
        }
    )
    [item] = split.ask_masks(photo.getvalue(), (2048, 1024))
    assert item["view"] == "front_right"
    assert item["fit"] == 1.0
    assert item["box"] == [204.8, 256.0, 819.2, 768.0]
    assert item["poly"][0] == (204.8, 256.0)
    [call] = fake_gen.calls
    assert call["raw_parts"][0] == MASK_ASK and call["schema"] == MASK_SCHEMA
    sent = Image.open(io.BytesIO(call["raw_parts"][1]))
    assert sent.size == (1024, 512)
    params = call["params"]
    assert params.model == get_models().judge
    assert (params.thinking, params.max_output_tokens, params.deadline_s) == (
        "MEDIUM",
        65535,
        240,
    )


def test_a_mask_answer_without_a_view_is_missing(fake_gen):
    photo = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(photo, format="PNG")
    box = {"box_2d": [0, 0, 500, 500], "mask": [[0, 0], [500, 0], [500, 500]]}
    fake_gen.json({"boxes": [box]})
    [item] = split.ask_masks(photo.getvalue(), (100, 100))
    assert item["view"] == "missing"


def test_to_masks_fills_each_polygon():
    items = [{"poly": [(2, 2), (5, 2), (5, 4), (2, 4)]}]
    [mask] = split.to_masks(items, 10, 8)
    assert mask.shape == (8, 10)
    assert mask.sum() == 4 * 3  # corners included


# ---------------------------------------------------------------- quality


def _quality(item):
    shape = (100, 200)
    shoe = _rect(shape, 40, 20, 120, 80)
    arr = _scene(shape, shoe)
    [mask] = split.to_masks([item], 200, 100)
    return split.quality(arr, [item], [mask])


def test_a_good_mask_passes():
    item = {"poly": [(40, 20), (119, 20), (119, 79), (40, 79)]}
    item.update(box=[40, 20, 119, 79], fit=1.0)
    scores = _quality(item)
    assert scores[0][1] == pytest.approx(1.0)
    assert split.bad(scores) == []


def test_a_drifted_mask_fails_on_its_fit():
    item = {"poly": [(40, 20), (119, 20), (119, 79), (40, 79)]}
    item.update(box=[40, 20, 119, 79], fit=0.6)
    assert split.bad(_quality(item)) == [0]


def test_a_tiny_mask_fails_on_its_coverage():
    item = {"poly": [(40, 20), (60, 20), (60, 40), (40, 40)]}  # a corner
    item.update(box=[40, 20, 119, 79], fit=0.9)
    scores = _quality(item)
    assert scores[0][1] < split.COVER_MIN
    assert split.bad(scores) == [0]


def test_coverage_leaves_out_the_other_masks():
    shape = (100, 200)
    a, b = _rect(shape, 20, 20, 100, 80), _rect(shape, 100, 20, 180, 80)
    items = [
        {"poly": [(20, 20), (99, 20), (99, 79), (20, 79)], "fit": 1.0},
        {"poly": [(100, 20), (179, 20), (179, 79), (100, 79)], "fit": 1.0},
    ]
    items[0]["box"] = [20, 20, 179, 79]  # its box reaches over the other shoe
    items[1]["box"] = [100, 20, 179, 79]
    scores = split.quality(
        _scene(shape, a, b), items, split.to_masks(items, 200, 100)
    )
    assert split.bad(scores) == []


# ---------------------------------------------------------------- grow


def test_grow_never_enters_the_other_mask_and_splits_the_gap():
    shape = (100, 300)
    a, b = _rect(shape, 20, 20, 140, 80), _rect(shape, 146, 20, 280, 80)
    ga, gb = split.grow([a, b])
    assert not (ga & b).any() and not (gb & a).any()
    assert not (ga & gb).any()  # a contested pixel goes to one mask
    assert ga.sum() > a.sum() and gb.sum() > b.sum()
    assert ga[50, 142] and gb[50, 144]  # the gap, each to the nearer one
    # 5 % of the long side (120 px): 6 px out.
    assert ga[50, 14] and not ga[50, 13]


# ---------------------------------------------------------------- cut_keep


def test_cut_keep_keeps_our_pixels_and_repaints_the_other_shoe():
    # Ours: an L whose box holds the other shoe, a big square. The square's
    # middle is far from any clean background: the old fill turned it black.
    shape = (400, 400)
    ours = _rect(shape, 20, 330, 380, 370) | _rect(shape, 20, 20, 60, 370)
    other = _rect(shape, 150, 60, 350, 260)
    arr = _scene(shape, ours, other)
    masks = [ours, other]
    cut = np.asarray(split.cut_keep(arr, masks, split.grow(masks), 0))
    assert cut.shape[:2] == (400, 400)  # its box: the whole photo
    assert (cut[ours] == SHOE).all()  # our pixels as they were
    assert cut[other].min() > 200  # the other shoe gone, no dark smear
    assert np.abs(cut[other].astype(int) - BG).max() <= 3


def test_cut_keep_crops_around_our_grown_mask():
    shape = (200, 400)
    ours, other = (
        _rect(shape, 40, 60, 140, 140),
        _rect(shape, 260, 60, 360, 140),
    )
    arr = _scene(shape, ours, other)
    grown = split.grow([ours, other])
    cut = split.cut_keep(arr, [ours, other], grown, 0)
    ys, xs = np.nonzero(grown[0])
    pad = int(0.04 * max(np.ptp(ys), np.ptp(xs)))
    width = xs.max() + pad - (xs.min() - pad)
    height = ys.max() + pad - (ys.min() - pad)
    assert cut.size == (width, height)
    assert (np.asarray(cut) == BG).all(axis=2).any()  # its real background
    assert not (np.asarray(cut) != BG).all(axis=2).all()


def test_cut_keep_with_one_shoe_is_a_plain_crop():
    shape = (100, 200)
    shoe = _rect(shape, 50, 30, 150, 70)
    arr = _scene(shape, shoe)
    arr[72:76, 50:150] = 200  # its shadow stays
    cut = np.asarray(split.cut_keep(arr, [shoe], split.grow([shoe]), 0))
    assert (cut == 200).all(axis=2).sum() == 4 * 100


def test_smooth_fill_far_from_clean_pixels_is_the_background_colour():
    clean = np.ones((400, 400), bool)
    clean[40:360, 40:360] = False  # a hole far wider than the blur
    img = np.full((400, 400, 3), BG, np.uint8)
    img[~clean] = 0
    model = split.smooth_fill(img, clean)
    assert np.abs(model[200, 200] - BG).max() < 1
