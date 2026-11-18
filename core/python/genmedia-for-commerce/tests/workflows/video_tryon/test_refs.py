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

"""Tests for the video references: try-on, back, face, product close-ups."""

from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.video_tryon.refs import (
    DETAILS,
    MAX_REFERENCES,
    add_details,
    build_references,
    find_details,
    product_photos,
    search_names,
)
from genmedia4commerce.workflows.video_tryon.types import (
    Closeup,
    Plan,
    ReferenceSet,
    Shot,
    VideoTryOnRequest,
)
from tests.workflows.video_tryon.conftest import BACK, FACE, TRYON, product_png


def products(*categories):
    return Classification(
        "outfit",
        [
            Product(
                chr(65 + i), c, "item", [ProductRef(product_png(i), "front")]
            )
            for i, c in enumerate(categories)
        ],
        "full_body",
    )


def request(**kw):
    return VideoTryOnRequest(tryon_image=TRYON, **kw)


def test_the_try_on_alone():
    refs = build_references(request(), face=None, framing=None)
    assert refs.images == [TRYON]
    assert (refs.framing, refs.back, refs.face, refs.closeups) == (
        "full_body",
        None,
        None,
        [],
    )


def test_order_try_on_back_face_then_products():
    photos = [product_png(0), product_png(1)]
    req = request(
        back_image=BACK,
        product_images=photos,
        classification=products("bag", "footwear"),
    )
    refs = build_references(req, face=FACE, framing="full_body")
    assert refs.images == [TRYON, BACK, FACE, *photos]
    assert (refs.back, refs.face) == (1, 2)
    assert [(c.index, c.name, c.worn) for c in refs.closeups] == [
        (3, "bag", "in the person's hand"),
        (4, "shoes", "on the person's feet"),
    ]


def test_two_products_of_a_kind_get_distinct_names():
    req = request(
        product_images=[product_png(0), product_png(1)],
        classification=products("bag", "bag"),
    )
    refs = build_references(req, face=None, framing="upper_body")
    assert [c.name for c in refs.closeups] == ["bag", "bag 2"]
    assert refs.framing == "upper_body"


def test_only_the_accessories_and_shoes_send_their_photos():
    """Garments come from the try-on image: their photos made the video
    model redraw them (a sleeveless coat got sleeves)."""
    photos = [product_png(i) for i in range(4)]
    req = request(
        back_image=BACK,
        product_images=photos,
        classification=products("full_body_outer", "bottom", "bag", "footwear"),
    )
    refs = build_references(req, face=FACE, framing=None)
    assert refs.images == [TRYON, BACK, FACE, photos[2], photos[3]]
    assert [(c.index, c.name) for c in refs.closeups] == [
        (3, "bag"),
        (4, "shoes"),
    ]
    assert product_photos(req) == photos[2:]  # what the search looks at


def test_unclassified_photos_all_go():
    photos = [product_png(0), product_png(1)]
    req = request(product_images=photos)  # categories unknown
    refs = build_references(req, face=None, framing=None)
    assert refs.images == [TRYON, *photos]
    assert product_photos(req) == photos
    assert search_names(req) == ["", ""]  # no product to name


def test_photos_without_a_matching_classification_are_products():
    req = request(
        product_images=[product_png(0), product_png(1)],
        classification=products("bag"),
    )
    refs = build_references(req, face=None, framing=None)
    assert [(c.name, c.worn) for c in refs.closeups] == [
        ("product", "worn by the person"),
        ("product 2", "worn by the person"),
    ]


def test_at_most_ten_images_the_last_photos_dropped():
    photos = [product_png(i) for i in range(12)]
    req = request(back_image=BACK, product_images=photos)
    refs = build_references(req, face=FACE, framing=None)
    assert len(refs.images) == MAX_REFERENCES == 10
    assert refs.images[3:] == photos[:7]
    assert [c.index for c in refs.closeups] == list(range(3, 10))


def worn_and_flat(*categories):
    """One product per category; the even ones photographed worn."""
    return Classification(
        "outfit",
        [
            Product(
                chr(65 + i),
                c,
                "item",
                [
                    ProductRef(
                        product_png(i),
                        "front",
                        presentation="worn" if i % 2 == 0 else "flat",
                    )
                ],
            )
            for i, c in enumerate(categories)
        ],
        "full_body",
    )


def test_only_worn_photos_get_their_face_blurred(blurred):
    req = request(
        product_images=[product_png(0), product_png(1)],
        classification=worn_and_flat("headwear", "bag"),
    )
    build_references(req, face=FACE, framing=None)
    assert blurred == [(product_png(0), False)]


def test_worn_glasses_keep_the_eyes_sharp(blurred):
    req = request(
        product_images=[product_png(0)],
        classification=worn_and_flat("eyewear"),
    )
    build_references(req, face=FACE, framing=None)
    assert blurred == [(product_png(0), True)]


def test_unclassified_photos_are_blurred_to_be_safe(blurred):
    req = request(product_images=[product_png(0), product_png(1)])
    build_references(req, face=FACE, framing=None)
    assert [keep for _, keep in blurred] == [False, False]


REAL_FIND = details.find  # before the ``zoom`` fixture fakes it
LOCK = Detail("hardware", "", "left", "padlock on the strap", b"lock")
STAMP = Detail("text", "MAKER", "front", "stamp at the bottom", b"stamp")


def shown(*names):
    """Two products (bag, shoes) and a plan with a close-up on ``names``."""
    refs = ReferenceSet(
        [TRYON, FACE, b"bag photo", b"shoes photo"],
        face=1,
        closeups=[
            Closeup(2, "bag", "in the person's hand", product="tote"),
            Closeup(3, "shoes", "on the person's feet", product="loafers"),
        ],
    )
    shots = [Shot("walk", 4.0)] + [Shot("detail", 3.0, n) for n in names]
    return refs, Plan(shots)


def test_a_close_up_product_gets_up_to_two_zoomed_details_last(zoom):
    zoom.found = [STAMP, LOCK, LOCK]
    refs, plan = shown("bag")
    out = add_details(refs, plan)
    assert zoom.calls == [([b"bag photo"], DETAILS, "tote")]  # its photo
    assert zoom.upscaled == [[STAMP, LOCK]]  # the two added, at once
    assert out.images == [*refs.images, b"stamp 2K", b"lock 2K"]
    bag, shoes = out.closeups
    assert bag.details == [
        (4, "stamp at the bottom"),
        (5, "padlock on the strap"),
    ]
    assert shoes.details == []
    assert refs.closeups[0].details == []  # the input is left as it was


def test_no_close_up_or_no_detail_found_changes_nothing(zoom):
    refs, plan = shown()
    assert add_details(refs, plan) == refs and zoom.calls == []
    refs, plan = shown("shoes")  # searched, nothing found
    assert add_details(refs, plan) == refs and len(zoom.calls) == 1
    assert zoom.upscaled == []  # nothing to upscale


def test_details_stay_within_the_reference_limit(zoom):
    zoom.found = [STAMP, LOCK]
    refs, plan = shown("bag", "shoes")
    refs.images += [b"more"] * (MAX_REFERENCES - len(refs.images) - 1)
    out = add_details(refs, plan)
    assert len(out.images) == MAX_REFERENCES
    assert zoom.calls[0][1] == 1  # one place left: one detail asked
    assert [c.details for c in out.closeups] == [
        [(MAX_REFERENCES - 1, "stamp at the bottom")],
        [],
    ]
    # both searched now; only the bag's, the one added, is upscaled
    assert len(zoom.calls) == 2 and zoom.upscaled == [[STAMP]]


def test_details_found_ahead_are_used_without_a_search(zoom):
    refs, plan = shown("shoes")
    out = add_details(refs, plan, found=[[STAMP], [LOCK]])
    assert zoom.calls == []  # searched during the image stage
    assert zoom.upscaled == [[LOCK]]  # the close-up product's only
    bag, shoes = out.closeups
    assert out.images == [*refs.images, b"lock 2K"]
    assert (bag.details, shoes.details) == ([], [(4, "padlock on the strap")])


def test_only_the_details_added_are_upscaled_in_one_call(zoom):
    refs, plan = shown("bag", "shoes")
    refs.images += [b"more"] * (MAX_REFERENCES - len(refs.images) - 3)
    found = [[STAMP, LOCK, LOCK], [LOCK, STAMP]]
    out = add_details(refs, plan, found=found)
    # three places left: the bag's first two, then one of the shoes'
    assert zoom.upscaled == [[STAMP, LOCK, LOCK]]
    assert out.images[-3:] == [b"stamp 2K", b"lock 2K", b"lock 2K"]
    assert [len(c.details) for c in out.closeups] == [2, 1]


def test_the_search_makes_no_image_model_call(fake_gen, monkeypatch):
    monkeypatch.setattr(details, "find", REAL_FIND)
    box = {"box_2d": [700, 400, 760, 600], "photo": 0, "legible": True}
    stamp = {"kind": "text", "text": "MAKER", "side": "front", **box}
    fake_gen.json({"details": [{**stamp, "where": "stamp"}]})
    photo = png(1000, 800)
    [[found]] = find_details([photo], ["tote"])
    assert (found.text, found.where) == ("MAKER", "stamp")
    assert found.image and found.image != photo  # the crop, not upscaled
    assert [c["fn"] for c in fake_gen.calls] == ["generate_json"]


def test_close_up_products_are_searched_at_once(zoom, monkeypatch):
    import threading

    both = threading.Barrier(2, timeout=5)  # each search waits for the other

    def find(images, *, limit=2, upscale=True, product=""):
        both.wait()
        return zoom(images, limit=limit, product=product)

    monkeypatch.setattr(details, "find", find)
    refs, plan = shown("bag", "shoes")
    add_details(refs, plan)
    assert sorted(images for images, *_ in zoom.calls) == [
        [b"bag photo"],
        [b"shoes photo"],
    ]


def test_each_photo_is_searched_with_its_products_name(zoom):
    """Told nothing, a search on a worn photo can return another garment's
    detail: each is told its product's description."""
    photos = [product_png(i) for i in range(3)]
    named = [("top", "field jacket"), ("bag", "tote"), ("footwear", "loafers")]
    req = request(
        product_images=photos,
        classification=Classification(
            "outfit",
            [
                Product(chr(65 + i), c, d, [ProductRef(photos[i], "front")])
                for i, (c, d) in enumerate(named)
            ],
            "full_body",
        ),
    )
    assert search_names(req) == ["tote", "loafers"]  # the photos sent
    refs = build_references(req, face=None, framing=None)
    assert [c.product for c in refs.closeups] == ["tote", "loafers"]
    plan = Plan([Shot("detail", 4.0, "bag"), Shot("detail", 4.0, "shoes")])
    add_details(refs, plan)  # nothing found ahead: searched now
    assert sorted((i, name) for i, _, name in zoom.calls) == sorted(
        [([photos[1]], "tote"), ([photos[2]], "loafers")]
    )
