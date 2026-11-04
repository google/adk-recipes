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

"""Tests for the try-on detail close-ups (one search per product, its
anchor first, the zoom photos, the wait, the labels and lines)."""

import asyncio
import threading
import time
from dataclasses import replace

import pytest

from genmedia4commerce.core import degraded, trace
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Photo,
    PhotoItem,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import closeups

PHOTOS = [png(8, 8, (1, 1, 1)), png(8, 8, (2, 2, 2))]
LOCK = Detail("hardware", "", "front", "padlock", b"lock")
STAMP = Detail("text", "MO", "front", "stamp", b"stamp")


def zooms(*images, view="front"):
    """Zoom photos (``Product.details``) of the part on ``view``."""
    return [ProductRef(image, view) for image in images]


def _classified(*products, extra=(), look=None):
    """(``products`` as the classifier saw them, its photos): one photo per
    upload, in order, listing its own product, then ``extra``, (image,
    ``Photo``) pairs; ``look``: the look photo's index."""
    images, photos = [], []
    for p in products:
        for r in p.refs:
            if r.source == "upload":
                item = PhotoItem(p.category, p.description, p.id)
                worn = r.presentation == "worn"
                photos.append(Photo(p.id, r.view, worn, False, [item]))
                images.append(r.image)
    for image, photo in extra:
        images.append(image)
        photos.append(photo)
    c = Classification("outfit", list(products), "full_body", look, photos)
    return c, images


def _searched(products, **photos):
    """Run the search of ``products`` (``_classified``); its result."""

    async def run():
        task = closeups.search(*_classified(*products, **photos))
        return await closeups.wait(task)

    return asyncio.run(run())


def _collected(products, task_result=None):
    """``collect`` for ``products`` after a search that returned
    ``task_result`` (None: no search)."""

    async def run():
        task = None
        if task_result is not None:
            task = asyncio.get_running_loop().create_future()
            task.set_result(task_result)
        return await closeups.collect(task, products)

    return asyncio.run(run())


@pytest.fixture
def finds(monkeypatch):
    """``details.find`` faked: each call recorded as (anchor, photos,
    limit, upscale, product); it returns the details ``answer`` gives for
    the product (none by default)."""
    calls, answer = [], {}

    def find(images, *, limit=2, upscale=True, product="", anchor=None):
        calls.append((anchor, list(images), limit, upscale, product))
        return answer.get(product, [])

    monkeypatch.setattr(details, "find", find)
    return calls, answer


def test_one_search_per_product_over_all_its_photos_but_the_back(finds):
    """Its anchor, the worn hero, first; then its other whole uploads
    (worn or flat, front or other) and zoom photos not of the back, told
    the product's name; never a back photo or a look crop; a product with
    no upload (one the look adds) is not searched."""
    calls, _ = finds
    worn, flat, side, back = b"W", b"F", b"S", b"B"
    crop = ProductRef(b"C", "front", 1, "worn", "model_crop")
    skirt = Product(
        "A",
        "bottom",
        "pencil skirt",
        [
            crop,
            ProductRef(worn, "front", 1, "worn"),
            ProductRef(flat, "front", 2),
            ProductRef(side, "other"),
            ProductRef(back, "back"),
        ],
        [
            *zooms(b"Z1"),
            *zooms(b"Z2", view="other"),
            *zooms(b"ZB", view="back"),
        ],
    )
    tee = Product("B", "top", "tee", [ProductRef(PHOTOS[0], "front")])
    added = Product("C", "footwear", "loafers", [crop])
    assert _searched([skirt, tee, added]) == [("A", []), ("B", [])]
    asked = closeups.SEARCHED  # merged, then cut to LIMIT_PER_PRODUCT
    assert sorted(calls, key=lambda c: c[4]) == [
        (worn, [flat, side, b"Z1", b"Z2"], asked, False, "pencil skirt"),
        (PHOTOS[0], [], asked, False, "tee"),
    ]
    assert (asked, closeups.LIMIT_PER_PRODUCT) == (5, 3)


def test_a_crop_heros_anchor_is_the_look_photo_else_a_front_photo(finds):
    """No worn upload: the photo its crop is cut from, known before the
    box calls, goes first: the look photo when it shows the product whole,
    else the first other front photo that does; its own photos follow."""
    calls, _ = finds
    jacket = Product(
        "A",
        "top",
        "jacket",
        [ProductRef(b"F", "front"), ProductRef(b"S", "other")],
    )
    shown = [PhotoItem("top", "jacket", "A")]
    look = (b"L", Photo(None, "front", True, False, shown))
    assert _searched([jacket], extra=[look], look=2) == [("A", [])]
    assert calls.pop() == (b"L", [b"F", b"S"], 5, False, "jacket")
    tee = Product("B", "top", "tee", [ProductRef(b"T", "front", 1, "worn")])
    other = (b"O", Photo("B", "front", True, False, shown))  # no look photo
    _searched([jacket, tee], extra=[other])
    assert sorted(calls, key=lambda c: c[4]) == [
        (b"O", [b"F", b"S"], 5, False, "jacket"),
        (b"T", [], 5, False, "tee"),  # its worn hero
    ]


def test_a_flat_or_fallback_heros_anchor_is_that_upload(finds):
    calls, _ = finds
    flat = Product(
        "A",
        "top",
        "tee",
        [ProductRef(b"S", "other"), ProductRef(b"F", "front")],
    )
    side = Product("B", "bag", "tote", [ProductRef(b"T", "other")])
    assert len(_searched([flat, side])) == 2
    assert sorted(calls, key=lambda c: c[4]) == [
        (b"F", [b"S"], 5, False, "tee"),  # its flat hero
        (b"T", [], 5, False, "tote"),  # its best upload
    ]


def test_at_most_four_searches_run_at_once(monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()

    def find(images, *, limit=2, upscale=True, product="", anchor=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return []

    monkeypatch.setattr(details, "find", find)
    products = [
        Product(
            f"P{k}", "top", "tee", [ProductRef(png(4, 4, (k, k, k)), "front")]
        )
        for k in range(7)
    ]
    assert [pid for pid, _ in _searched(products)] == [p.id for p in products]
    assert peak[0] == closeups.MAX_SEARCHES == 4


def test_the_search_is_off_or_has_nothing_to_search(finds, monkeypatch):
    calls, _ = finds
    only_back = Product("A", "top", "tee", [ProductRef(b"B", "back")])

    async def start(products):
        return closeups.search(*_classified(*products))

    assert asyncio.run(start([only_back])) is None
    flat = Product("C", "top", "tee", [ProductRef(PHOTOS[0], "front")])
    monkeypatch.setenv("GENMEDIA_TRYON_CLOSEUPS", "false")
    get_settings.cache_clear()
    try:
        assert asyncio.run(start([flat])) is None
    finally:
        get_settings.cache_clear()
    assert calls == []


def test_a_zoom_photos_close_up_is_a_photo_kept_only_when_small(
    finds, monkeypatch
):
    """Each detail is cropped from its sharpest photo: the anchor's or a
    whole upload's stays a crop, a zoom photo's becomes ``PHOTO`` and is
    dropped when larger than ``MAX_ZOOM_SHARE`` of it; the order is the
    search's."""
    saved = []
    monkeypatch.setattr(trace, "save", lambda label, data: saved.append(label))
    calls, answer = finds
    skirt = Product(
        "A",
        "bottom",
        "skirt",
        [ProductRef(PHOTOS[0], "front"), ProductRef(PHOTOS[1], "other")],
        zooms(b"Z1", b"Z2"),
    )
    buckle = Detail(
        "hardware",
        "AURELIA",
        "front",
        "belt",
        b"buckle",
        photo=2,  # the first zoom photo, after the anchor and the side
        box=(300, 300, 700, 650),  # 40% of it: kept
        name="buckle",
    )
    lapel = Detail(
        "hardware", "", "front", "lapel", b"l", photo=3, box=(50, 0, 460, 1)
    )
    stamp = replace(STAMP, photo=1)  # the side upload
    answer["skirt"] = [buckle, stamp, lapel]
    found = _searched([skirt])
    assert calls[0][:2] == (PHOTOS[0], [PHOTOS[1], b"Z1", b"Z2"])
    assert found == [("A", [replace(buckle, kind=closeups.PHOTO), stamp])]
    assert _collected([skirt], found) == [
        ("A", replace(buckle, kind=closeups.PHOTO)),
        ("A", stamp),
    ]
    assert saved == ["closeup-A-0", "closeup-A-1"]
    photo = _collected([skirt], found)[0][1]
    assert closeups.label("A1", photo) == "A1: AURELIA buckle"


def test_without_a_search_the_zoom_photos_go_as_they_are():
    """Search off or failed: each zoom photo not of the back is a close-up
    as it is; with none, no close-ups at all (None)."""
    skirt = Product(
        "A",
        "bottom",
        "pencil skirt",
        [ProductRef(PHOTOS[0], "front")],
        [*zooms(b"P1"), *zooms(b"PB", view="back")],
    )
    tee = Product("B", "top", "tee", [ProductRef(PHOTOS[1], "front")])
    where = "close-up of the pencil skirt"
    ((pid, detail),) = _collected([skirt, tee])
    assert (pid, detail) == (
        "A",
        Detail(closeups.PHOTO, "", "other", where, b"P1"),
    )
    assert closeups.label("A1", detail) == "A1: close-up"
    assert closeups.item("A1", detail) == "A1 close-up of the pencil skirt"
    assert _collected([tee]) is None
    assert _collected([tee], [("B", [])]) == []  # searched, nothing found


def test_a_failed_product_search_sends_its_zoom_photos_as_they_are(
    monkeypatch,
):
    def find(images, *, limit=2, upscale=True, product="", anchor=None):
        if product == "skirt":
            raise RuntimeError("judge down")
        return [LOCK]

    monkeypatch.setattr(details, "find", find)
    skirt = Product(
        "A", "bottom", "skirt", [ProductRef(PHOTOS[0], "front")], zooms(b"Z")
    )
    tee = Product("B", "top", "tee", [ProductRef(PHOTOS[1], "front")])
    with degraded.tracking() as marker:
        found = _searched([skirt, tee])
    assert found == [("B", [LOCK])]
    assert not marker.ok
    where = "close-up of the skirt"
    assert _collected([skirt, tee], found) == [
        ("A", Detail(closeups.PHOTO, "", "other", where, b"Z")),
        ("B", LOCK),
    ]


def test_wait_returns_the_closeups_once_the_search_is_over():
    async def found():
        await asyncio.sleep(0.05)  # still running when the wait starts
        return [("A", [LOCK]), ("B", [])]

    async def broken():
        raise RuntimeError("search down")

    async def run(make):
        return await closeups.wait(asyncio.ensure_future(make()))

    assert asyncio.run(closeups.wait(None)) == []
    assert asyncio.run(run(found)) == [("A", [LOCK]), ("B", [])]
    assert asyncio.run(run(broken)) == []  # drawn without close-ups


@pytest.mark.parametrize(
    ("kind", "text", "where", "name", "expected"),
    [
        ("hardware", "", "top of right side seam", "side zip", "A1: side zip"),
        ("hardware", "MO", "upper strap", "padlock", "A1: MO padlock"),
        ("logo", "MO", "tongue", "MO logo", "A1: MO logo"),  # MO once
        ("hardware", "", "top of right side seam", "", "A1: top"),  # no name
    ],
)
def test_a_label_says_what_the_detail_is(kind, text, where, name, expected):
    detail = Detail(kind, text, "front", where, b"", name=name)
    assert closeups.label("A1", detail) == expected


def test_a_detail_photo_line_says_it_is_a_photo():
    photo = Detail(
        closeups.PHOTO, "", "other", "AURELIA plaque on the belt", b"P"
    )
    (photo_line, image), (crop_line, _) = closeups.lines(
        [("A", photo), ("A", STAMP)], {"A": "PRODUCT A skirt"}
    )
    assert image == b"P"
    assert photo_line.startswith(
        "DETAIL CLOSE-UP of PRODUCT A skirt (AURELIA plaque on the belt): a "
        "close-up photo of the product; keep its shape"
    )
    assert crop_line.startswith(
        "DETAIL CLOSE-UP of PRODUCT A skirt (text 'MO'; stamp): a crop of "
        "its product photo, shown enlarged; keep its shape"
    )


SHORTENED = (
    "MAISON ORVELLE PARIS\u2026 stamp (lettering 'MAISON ORVELLE PARIS MADE')"
)


@pytest.mark.parametrize(
    ("text", "name", "where", "expected"),
    [
        (
            "AURELIA MILANO",
            "buckle",
            "at the waist",
            "AURELIA MILANO buckle, at the waist",
        ),
        ("", "button", "button on the jeans", "button on the jeans"),
        ("MO", "padlock", "padlock", "MO padlock"),
        ("MAISON ORVELLE PARIS MADE", "stamp", "strap", f"{SHORTENED}, strap"),
    ],
)
def test_a_zoom_photos_line_names_it_like_its_card(text, name, where, expected):
    """A single product's close-up found in a zoom photo shows its name and
    lettering, as an outfit card's label does, then where it is."""
    detail = Detail(closeups.PHOTO, text, "front", where, b"Z", name=name)
    ((line, _),) = closeups.lines([("A", detail)], {"A": "PRODUCT A skirt"})
    assert line.startswith(
        f"DETAIL CLOSE-UP of PRODUCT A skirt ({expected}): a close-up photo "
        "of the product; keep its shape"
    )
