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

"""Tests for the merge of a product's close-ups that would show nearly the
same area (photo try-on only), the limit after it, and their labels."""

from dataclasses import replace

import pytest

from genmedia4commerce.core import media
from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import closeup_merge, closeups

PHOTO = png(1000, 1000, (90, 60, 30))
OTHER = png(1000, 1000, (30, 60, 90))
# A label right above a triangle patch: their crops share 65% of the
# patch's; a zip pull at the hem shares nothing with them.
LABEL = Detail(
    "label",
    "AURELIA MILANO",
    "front",
    "label at the back neck",
    b"label",
    box=(100, 400, 160, 600),
    name="label",
)
PATCH = Detail(
    "logo",
    "",
    "front",
    "patch below the label",
    b"patch",
    box=(180, 450, 260, 550),
    name="triangle patch",
)
PULL = Detail(
    "hardware",
    "",
    "front",
    "zip pull at the hem",
    b"pull",
    box=(850, 100, 900, 150),
    name="zip pull",
)
UNION = (100, 400, 260, 600)


def _merged_label() -> Detail:
    """LABEL and PATCH as one close-up, cut again from PHOTO."""
    image = details.crop(details.open_photo(PHOTO), UNION)
    return replace(
        LABEL,
        kind="logo",
        image=image,
        box=UNION,
        name="label and triangle patch",
    )


def test_two_close_ups_whose_crops_overlap_become_one():
    """The union box, cut again with the same rules; both names, the
    lettering, the text or logo kind, the first's side and where."""
    found = closeup_merge.merge([LABEL, PATCH, PULL], [PHOTO])
    assert found == [_merged_label(), PULL]
    left, top, right, bottom = details.crop_rect((1000, 1000), UNION)
    assert media.image_size(found[0].image) == (right - left, bottom - top)


def test_close_ups_far_apart_or_of_another_photo_stay():
    other = replace(PATCH, photo=1)  # the same place, another photo
    found = [LABEL, other, PULL]
    assert closeup_merge.merge(found, [PHOTO, OTHER]) == found
    assert closeup_merge.merge([], [PHOTO]) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("MO", "AURELIA MILANO, MO"),  # every lettering
        ("aurelia", "AURELIA MILANO"),  # held by the other: once
    ],
)
def test_a_merged_close_up_keeps_every_lettering(text, expected):
    (merged,) = closeup_merge.merge([LABEL, replace(PATCH, text=text)], [PHOTO])
    assert (merged.text, merged.kind) == (expected, "logo")


def test_a_merge_refused_by_fits_stays_two():
    found = closeup_merge.merge([LABEL, PATCH], [PHOTO], fits=lambda _: False)
    assert found == [LABEL, PATCH]


def _search_one(monkeypatch, found, zooms=()):
    """``closeups._search_one`` of a product with PHOTO as its anchor and
    ``zooms``, the search finding ``found``; (its close-ups, the limit
    the search was asked for)."""
    asked = []

    def find(images, *, limit=2, upscale=True, product="", anchor=None):
        asked.append(limit)
        return found

    monkeypatch.setattr(details, "find", find)
    result = closeups._search_one("A", "jacket", PHOTO, [], list(zooms))
    assert result is not None
    return result[1], asked


def test_the_limit_applies_after_merging(monkeypatch):
    """Five asked, merged, then three kept: the slot the merge frees holds
    the fourth detail."""
    stud = replace(PULL, box=(850, 850, 900, 900), name="stud")
    rivet = replace(PULL, box=(500, 50, 550, 100), name="rivet")
    found = [LABEL, PATCH, PULL, stud, rivet]
    kept, asked = _search_one(monkeypatch, found)
    assert asked == [closeups.SEARCHED] == [5]
    assert kept == [_merged_label(), PULL, stud]


def test_a_zoom_photos_merged_close_up_stays_within_its_share(monkeypatch):
    """Two details of a zoom photo merge only while the union stays within
    ``MAX_ZOOM_SHARE`` of it (a larger one is no detail); each is of
    ``PHOTO`` kind."""
    zoom = png(1000, 1000, (10, 10, 10))
    near = [
        replace(LABEL, photo=1, box=(100, 100, 300, 300)),
        replace(PATCH, photo=1, box=(150, 150, 350, 350)),
    ]
    far = [near[0], replace(PATCH, photo=1, box=(350, 350, 650, 650))]
    (merged,), _ = _search_one(monkeypatch, near, [zoom])
    assert (merged.kind, merged.box) == (closeups.PHOTO, (100, 100, 350, 350))
    kept, _ = _search_one(monkeypatch, far, [zoom])
    assert [d.box for d in kept] == [d.box for d in far]
    assert {d.kind for d in kept} == {closeups.PHOTO}


@pytest.mark.parametrize(
    ("text", "name", "label", "item"),
    [
        (
            "AURELIA MILANO",
            "label and triangle patch",
            "A1: AURELIA MILANO label and triangle patch",
            "A1 AURELIA MILANO label and triangle patch at the back neck",
        ),
        (
            "AURELIA MILANO",
            "brand label and triangle patch",
            "A1: AURELIA MILANO brand label and triangle patch",
            "A1 AURELIA MILANO brand label and triangle patch at the back neck",
        ),
        (
            "",
            "brass zip pull and side buckle",
            "A1: brass zip pull and side buckle",
            "A1 brass zip pull and side buckle (label at the back neck)",
        ),
        (
            "AURELIA MILANO, KORVEN'S PARIS",
            "label and logo",
            "A1: AURELIA MILANO, KORVEN'S\u2026 label and logo",
            "A1 AURELIA MILANO, KORVEN'S\u2026 label and logo at the back "
            "neck (lettering 'AURELIA MILANO, KORVEN'S PARIS')",
        ),
    ],
)
def test_a_merged_close_ups_label_and_item_name_both_whole(
    text, name, label, item
):
    merged = replace(LABEL, text=text, name=name)
    assert closeups.label("A1", merged) == label
    assert closeups.item("A1", merged) == item


def test_a_single_products_merged_close_up_line():
    ((line, image),) = closeups.lines(
        [("A", _merged_label())], {"A": "PRODUCT A jacket"}
    )
    assert image == _merged_label().image
    assert line.startswith(
        "DETAIL CLOSE-UP of PRODUCT A jacket (label and triangle patch "
        "'AURELIA MILANO'; label at the back neck): a crop of its product "
        "photo, shown enlarged"
    )


def test_a_merge_too_long_for_the_card_stays_two():
    """A merged crop longer than the card cap and both its parts would show
    them smaller than apart: refused. One no longer than its longer part (a
    large zoom-photo close-up) is kept."""
    from genmedia4commerce.workflows.details import Detail
    from genmedia4commerce.workflows.tryon.closeup_merge import too_long

    def detail(box: tuple[int, int, int, int]) -> Detail:
        return Detail("logo", "", "front", "chest", b"", box=box)

    size, cap = (2400, 3000), 700
    tab, patch = detail((219, 687, 250, 843)), detail((250, 672, 360, 868))
    both = detail((219, 672, 360, 868))  # as long as the patch's crop
    assert not too_long(size, both, tab, patch, cap)
    a, b = detail((100, 450, 130, 500)), detail((300, 450, 330, 500))
    apart = detail((100, 450, 330, 500))  # 2.75 x a tall union: > cap
    assert too_long(size, apart, a, b, cap)
