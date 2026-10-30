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

"""Tests for the try-on classifier: one row per photo, grouped into products."""

import pytest

from genmedia4commerce.core import degraded
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import FRAMING_BY_CATEGORY
from genmedia4commerce.workflows.tryon.classify import classify_products

FULL_BODY_LOOK = "full_body_look"


def row(c, g, s="front", w=False, d=None, **extra):
    return {"c": c, "d": d or f"{c} item", "s": s, "w": w, "g": g, **extra}


def look(g, s="front"):
    """A full-body look photo's row."""
    return row(FULL_BODY_LOOK, g, s, True, "full model reference")


def garments(*spans):
    """The answer's "garments": (group, starts, ends) each."""
    return [{"g": g, "starts": a, "ends": b} for g, a, b in spans]


def refs(product):
    return [(r.view, r.presentation, r.rank, r.source) for r in product.refs]


def test_one_call_over_the_photos_low_thinking(fake_gen, images):
    fake_gen.json({"photos": [row("top", 1), row("top", 1, "back")]})
    classify_products(images[:2])
    (call,) = fake_gen.calls
    assert call["raw_parts"] == ["IMAGE_0:", images[0], "IMAGE_1:", images[1]]
    assert call["params"].thinking == "LOW"
    assert call["params"].model == get_models().judge
    assert call["schema"]["properties"]["photos"]["type"] == "ARRAY"
    assert (
        "You receive 2 product photo(s), labelled IMAGE_0 to IMAGE_1"
        in (call["system"])
    )


def test_a_single_product(fake_gen, images):
    fake_gen.json(
        {
            "photos": [
                row("top", 1, "front", False, "white tee"),
                row("top", 1, "back", True, "white tee"),
            ]
        }
    )
    result = classify_products(images[:2])
    assert (result.mode, result.framing) == ("single", "upper_body")
    (product,) = result.products
    assert (product.id, product.category, product.description) == (
        "A",
        "top",
        "white tee",
    )
    assert [r.image for r in product.refs] == images[:2]
    assert refs(product) == [
        ("front", "flat", 1, "upload"),
        ("back", "worn", 1, "upload"),
    ]


def test_groups_become_products_in_order_of_first_appearance(fake_gen, images):
    fake_gen.json(
        {"photos": [row("bottom", 7), row("top", 2), row("bottom", 7, w=True)]}
    )
    result = classify_products(images[:3])
    assert (result.mode, result.framing) == ("outfit", "full_body")
    assert [(p.id, p.category) for p in result.products] == [
        ("A", "bottom"),
        ("B", "top"),
    ]
    assert [r.image for r in result.products[0].refs] == [images[0], images[2]]


def test_photos_of_one_side_rank_in_upload_order(fake_gen, images):
    fake_gen.json(
        {"photos": [row("top", 1), row("top", 1, "back"), row("top", 1)]}
    )
    (product,) = classify_products(images[:3]).products
    assert [(r.view, r.rank) for r in product.refs] == [
        ("front", 1),
        ("back", 1),
        ("front", 2),
    ]


def test_a_group_takes_its_most_common_category(fake_gen, images):
    """A full look filed with the trousers stays trousers."""
    fake_gen.json(
        {
            "photos": [
                row("bottom", 1, d="grey trousers"),
                row("top", 1, w=True, d="blue turtleneck"),
                row("bottom", 1, d="grey wool trousers"),
            ]
        }
    )
    (product,) = classify_products(images[:3]).products
    assert (product.category, product.description) == (
        "bottom",
        "grey trousers",
    )
    assert len(product.refs) == 3


def test_broken_fields_are_normalised(fake_gen, images, caplog):
    fake_gen.json(
        {
            "photos": [
                {"c": "cape", "d": "x", "s": "diagonal", "w": "yes", "g": 1},
                {"c": "top", "d": "y", "s": "front", "w": True},  # no group
            ],
            "garments": garments((1, "shoulders", "hips")),
        }
    )
    with degraded.tracking() as marker:
        result = classify_products(images[:2])
    first, second = result.products
    assert (first.category, refs(first)) == (
        "top",
        [("other", "flat", 1, "upload")],
    )
    assert "no valid category" in caplog.text
    # with no group, the second can have no span either
    assert marker.reasons == [
        "classifier gave a product no valid category",
        "classifier gave a garment no valid start and end",
    ]
    assert (first.span, second.span) == (("shoulders", "hips"), None)
    assert (second.category, refs(second)) == (
        "top",
        [("front", "worn", 1, "upload")],
    )


def test_a_skipped_photo_joins_the_first_product_as_other(
    fake_gen, images, caplog
):
    fake_gen.json(
        {
            "photos": [row("bottom", 1), row("top", 2)],
            "garments": garments(
                (1, "waist", "ankles"), (2, "shoulders", "hips")
            ),
        }
    )
    with degraded.tracking() as marker:
        result = classify_products(images[:3])
    assert marker.reasons == ["classifier skipped a photo"]
    assert [p.category for p in result.products] == ["bottom", "top"]
    assert result.products[0].refs[-1].image == images[2]
    assert refs(result.products[0])[-1] == ("other", "flat", 2, "upload")
    assert "skipped photo 2" in caplog.text


@pytest.mark.parametrize(
    "raw", [{}, {"photos": []}, {"photos": [{}, None]}, "x"]
)
def test_no_rows_raises(fake_gen, images, raw):
    fake_gen.json(raw)
    with pytest.raises(GenerationError):
        classify_products(images[:2])


@pytest.mark.parametrize("category", list(FRAMING_BY_CATEGORY))
def test_framing_for_every_category(fake_gen, images, category):
    fake_gen.json({"photos": [row(category, 1)]})
    assert (
        classify_products(images[:1]).framing == FRAMING_BY_CATEGORY[category]
    )


def test_the_prompt_asks_for_close_ups_and_files_belts_with_their_bottom(
    fake_gen, images
):
    fake_gen.json({"photos": [row("top", 1)]})
    classify_products(images[:1])
    (call,) = fake_gen.calls
    items = call["schema"]["properties"]["photos"]["items"]
    assert "x" in items["required"]
    assert "l" not in items["properties"] and "l" not in items["required"]
    assert items["properties"]["c"]["enum"][-1] == FULL_BODY_LOOK
    assert "zl" not in items["properties"] and '"zl"' not in call["system"]
    assert '"label"' not in call["system"]
    assert '"l"' not in call["system"]
    assert (
        'A "full_body_look" photo: "full model reference".' in call["system"]
        and 'A "full_body_look" photo is a reference, not a product photo: '
        "its group is -1."
        in call["system"]
        and "FULL-BODY LOOK: a photo of a person seen from head to feet"
        in call["system"]
        and ", bag, full_body_look." in call["system"]
    )
    assert '"x": true when the photo shows no product in full' in call["system"]
    assert (
        "A belt is part of the bottom it is on (skirt, trousers), never a product of its own"
        in call["system"]
    )
    assert "REFERENCE_" not in call["system"]


def test_any_back_is_back_and_a_zoom_photo_gets_the_side_it_frames(
    fake_gen, images
):
    fake_gen.json({"photos": [row("top", 1)]})
    classify_products(images[:1])
    system = fake_gen.calls[0]["system"]
    assert (
        '- "back": any view of the back of the product, straight or turned, '
        "even slightly." in system
    )
    assert (
        'For a zoom photo ("x" true): the part it frames is on the front of '
        "the product."
        in system
        and "For a zoom photo: the part it frames is on the back of the "
        "product."
        in system
        and "For a zoom photo: the part it frames is on a side of the "
        "product, or has no side (a fabric swatch)."
        in system
    )
    assert "detail close-up" not in system
    assert "Slightly angled back views" not in system


def test_close_ups_are_detail_photos_not_refs(fake_gen, images):
    fake_gen.json(
        {
            "photos": [
                row("bottom", 1, w=True, d="pencil skirt"),
                row("bottom", 1, "back", True, x=True),
                row("bottom", 1, "back"),
                row("top", 2),
                row("top", 2, "other", x=True),
                row("top", 2, "front", x=True),
            ]
        }
    )
    skirt, top = classify_products(images[:6]).products
    assert [r.image for r in skirt.refs] == [images[0], images[2]]
    assert refs(skirt) == [
        ("front", "worn", 1, "upload"),
        ("back", "flat", 1, "upload"),
    ]
    # each zoom photo keeps the side of the part it frames
    assert [r.image for r in skirt.details] == [images[1]]
    assert [r.image for r in top.details] == [images[4], images[5]]
    assert [(r.view, r.presentation, r.rank) for r in skirt.details] == [
        ("back", "worn", 1)
    ]
    assert [r.view for r in top.details] == ["other", "front"]


def test_a_group_of_close_ups_only_keeps_them_as_refs(fake_gen, images):
    fake_gen.json({"photos": [row("bag", 1, "other", x=True)]})
    (bag,) = classify_products(images[:1]).products
    assert [r.image for r in bag.refs] == [images[0]] and bag.details == []


def test_the_first_front_photo_of_a_whole_look_is_the_look_photo(
    fake_gen, images
):
    fake_gen.json(
        {
            "photos": [
                row("bottom", 1),
                look(1, "back"),  # from the back
                look(1),
                look(1),
            ]
        }
    )
    result = classify_products(images[:4])
    assert result.look_photo == 2
    # never a photo of its product; the back one is its back photo (single)
    assert [(r.image, r.view) for r in result.products[0].refs] == [
        (images[0], "front"),
        (images[1], "back"),
    ]


def test_a_turned_whole_look_is_the_look_photo_when_none_faces_front(
    fake_gen, images
):
    """A three-quarter full-body photo reads "other": the cut takes it when
    no full-body photo faces the camera, and a front one wins."""
    turned, front = look(-1, "other"), look(-1, "front")
    fake_gen.json({"photos": [row("bottom", 1), turned]})
    assert classify_products(images[:2]).look_photo == 1
    fake_gen.json({"photos": [row("bottom", 1), turned, front]})
    assert classify_products(images[:3]).look_photo == 2


def test_a_look_photo_is_never_a_product_photo_whatever_its_group(
    fake_gen, images
):
    """Its group and name are ignored."""
    fake_gen.json(
        {
            "photos": [
                look(1),
                row("top", 1, d="polo shirt"),
                look(1),
                look(-1),
            ]
        }
    )
    result = classify_products(images[:4])
    (polo,) = result.products
    assert (polo.id, polo.category, polo.description) == (
        "A",
        "top",
        "polo shirt",
    )
    assert [r.image for r in polo.refs] == [images[1]]
    assert (result.mode, result.framing, result.look_photo) == (
        "single",
        "upper_body",
        0,
    )


def test_a_full_body_look_is_never_a_product_nor_a_category_vote(
    fake_gen, images
):
    """Look rows outnumbering a group's own rows never join it, vote for
    its category or make a product of their own."""
    fake_gen.json(
        {
            "photos": [
                look(1, "other"),
                row("bottom", 1, d="pleated skirt"),
                look(1, "other"),
                look(1, "other"),
                look(2, "other"),
            ],
            "garments": garments((1, "waist", "knees")),
        }
    )
    with degraded.tracking() as marker:
        result = classify_products(images[:5])
    (skirt,) = result.products
    assert (skirt.category, skirt.description) == ("bottom", "pleated skirt")
    assert [r.image for r in skirt.refs] == [images[1]]
    # the first turned look photo is the look photo (none faces front)
    assert (result.framing, result.look_photo) == ("lower_body", 0)
    assert marker.reasons == []


def test_the_back_look_photo_is_dropped_unless_one_product_has_a_front(
    fake_gen, images
):
    back = look(1, "back")
    fake_gen.json({"photos": [row("top", 1), row("bottom", 2), back]})
    result = classify_products(images[:3])
    assert [len(p.refs) for p in result.products] == [1, 1]  # an outfit
    # one product without a front photo: it would be its main photo
    fake_gen.json({"photos": [row("top", 1, "other"), back]})
    (product,) = classify_products(images[:2]).products
    assert [r.image for r in product.refs] == [images[0]]


def test_only_look_photos_leave_no_product(fake_gen, images, caplog):
    """A lone look photo: ``reference`` cuts it into products."""
    fake_gen.json(
        {
            "photos": [
                look(1),
                look(1, "back"),
            ]
        }
    )
    result = classify_products(images[:3])  # photo 2 skipped: dropped
    assert (result.products, result.look_photo) == ([], 0)
    assert (result.mode, result.framing) == ("outfit", "full_body")
    assert "skipped photo 2; dropped" in caplog.text


def test_no_look_photo_without_one(fake_gen, images):
    fake_gen.json({"photos": [row("top", 1, w=True)]})
    assert classify_products(images[:1]).look_photo is None


ITEMS = (
    'ITEMS: for every photo, "items" lists every product shown whole inside '
    "the frame, one entry per product: garments, shoes, bags, hats, glasses, "
    "jewellery and scarves. A part hidden by another garment still counts as "
    "inside the frame; a product cut off by the frame is never listed. For "
    'each item: "c" its category (a product category, never '
    '"full_body_look"), "d" its short name (the same rules as "d" above), '
    '"g" its group. "covered" is true when another garment hides most of '
    "the item (an inner layer under a jacket or coat, a shirt showing only "
    "its collar), else false. The group is the group of the uploaded "
    "product when the item is that product: the same item as in the other "
    "photos, even worn open instead of closed, seen from another side, or "
    "partly covered. "
    "Every part of an uploaded product is that product: its lining, fur, "
    "hood, collar, cuffs or belt are never items of their own. Any other "
    "item gets a new group number, after every group of the photos; the "
    "same item in two photos has the same group. A belt worn on a bottom is "
    'part of that bottom. For a "full_body_look" photo whose "s" is "back", '
    '"items" is [].'
)


def test_every_row_lists_the_products_shown_whole(fake_gen, images):
    fake_gen.json({"photos": [row("top", 1)]})
    classify_products(images[:1])
    (call,) = fake_gen.calls
    assert f'"x" is false.\n\n{ITEMS}\n\nGARMENTS:' in call["system"]
    photo = call["schema"]["properties"]["photos"]["items"]
    assert photo["required"][-1] == "items"
    item = photo["properties"]["items"]["items"]
    assert item["required"] == ["c", "d", "g", "covered"]
    assert item["properties"]["covered"] == {"type": "BOOLEAN"}
    assert FULL_BODY_LOOK not in item["properties"]["c"]["enum"]
    assert "top" in item["properties"]["c"]["enum"]


def _items(*items):
    return [{"c": c, "d": d, "g": g} for c, d, g in items]


def test_items_name_their_uploaded_product_else_their_own_group(
    fake_gen, images
):
    fake_gen.json(
        {
            "photos": [
                row("bottom", 4, items=_items(("bottom", "skirt", 4))),
                row("top", 2, w=True, x=True, items=_items(("bag", "tote", 9))),
                {
                    **look(-1),
                    "items": [
                        {"c": "top", "d": "shirt", "g": 2, "covered": True},
                        *_items(("bag", "tote", 9)),
                        {"c": "cape", "d": "x", "g": 5},  # no category
                        {"c": "top", "d": "x", "g": True},  # no group
                        "not an item",
                    ],
                },
            ]
        }
    )
    result = classify_products(images[:3])
    assert [p.id for p in result.products] == ["A", "B"]
    skirt, zoom, outfit = result.photos
    assert (skirt.product, skirt.view, skirt.worn, skirt.zoom) == (
        "A",
        "front",
        False,
        False,
    )
    assert [(i.category, i.name, i.product) for i in skirt.items] == [
        ("bottom", "skirt", "A")
    ]
    assert (zoom.product, zoom.worn, zoom.zoom) == ("B", True, True)
    assert outfit.product is None and outfit.worn
    assert [(i.name, i.product, i.covered) for i in outfit.items] == [
        ("shirt", "B", True),  # under another garment
        ("tote", "g9", False),  # nobody uploaded it: its own group
    ]
    assert result.look_photo == 2


def look_row(items=(), c=FULL_BODY_LOOK, s="front"):
    return {**row(c, -1, s, True), "items": _items(*items)}


def test_the_reference_model_goes_last_and_is_the_look_photo(fake_gen, images):
    """Whatever its row says, never a product's photo; it wins over a look
    photo among the uploads."""
    model = images[5]
    fake_gen.json(
        {
            "photos": [
                row("top", 1),
                look(-1),
                look_row([("top", "top item", 1)], c="top"),
            ]
        }
    )
    result = classify_products(images[:2], model)
    (call,) = fake_gen.calls
    assert call["raw_parts"][-2:] == ["IMAGE_2:", model]
    assert "You receive 3 product photo(s)" in call["system"]
    (top,) = result.products
    assert [r.image for r in top.refs] == [images[0]]
    assert (result.look_photo, len(result.photos)) == (2, 3)
    assert result.photos[2].product is None
    assert [i.product for i in result.photos[2].items] == ["A"]


def test_with_only_look_photos_uploaded_the_reference_model_is_left_out(
    fake_gen, images
):
    """A lone look photo: the uploaded look is the outfit to try on."""
    fake_gen.json(
        {
            "photos": [
                look_row([("bottom", "skirt", 1)]),
                look_row([("bag", "tote", 1)]),
            ]
        }
    )
    result = classify_products(images[:1], images[5])
    assert (result.products, result.look_photo) == ([], 0)
    assert len(result.photos) == 1


LANDMARKS = [
    "head",
    "chin",
    "shoulders",
    "chest",
    "waist",
    "hips",
    "mid-thigh",
    "knees",
    "mid-calf",
    "ankles",
    "feet",
]


def test_the_schema_requires_where_each_garment_starts_and_ends(
    fake_gen, images
):
    """In the same call, after the photos: one entry per garment group,
    each on the fixed scale."""
    fake_gen.json({"photos": [row("top", 1)]})
    classify_products(images[:1])
    (call,) = fake_gen.calls
    schema = call["schema"]
    assert schema["required"] == ["photos", "garments"]
    assert schema["propertyOrdering"] == ["photos", "garments"]
    entry = schema["properties"]["garments"]["items"]
    assert entry["required"] == ["g", "starts", "ends"]
    assert entry["properties"]["g"] == {"type": "INTEGER"}
    for key in ("starts", "ends"):
        assert entry["properties"][key] == {
            "type": "STRING",
            "enum": LANDMARKS,
        }
    assert (
        'GARMENTS: "garments" has one entry per group of photos whose '
        'category is "top", "bottom", "dress" or "full_body_outer", none for '
        'any other: "g" its group, "starts" the highest point and "ends" the '
        "lowest point the garment reaches on a standing person, as its "
        "photos show it, each one of, from the top down: "
        f"{', '.join(LANDMARKS)}. Shoulder straps and a bib start at the "
        "shoulders."
    ) in call["system"]


def test_each_garment_gets_its_span_and_no_other_product(
    fake_gen, images, caplog
):
    fake_gen.json(
        {
            "photos": [
                row("dress", 1, d="cycling bib shorts"),
                row("headwear", 2),
                row("bottom", 3),
            ],
            "garments": garments(
                (1, "shoulders", "mid-thigh"),
                (2, "head", "chin"),  # not a garment: ignored
                (3, "waist", "ankles"),
                (3, "waist", "knees"),  # a second entry: the first wins
                (9, "chest", "hips"),  # no such product
            ),
        }
    )
    with degraded.tracking() as marker:
        bib, cap, trousers = classify_products(images[:3]).products
    assert bib.span == ("shoulders", "mid-thigh")
    assert cap.span is None
    assert trousers.span == ("waist", "ankles")
    assert marker.reasons == []
    assert "no valid span" not in caplog.text


@pytest.mark.parametrize(
    "answer",
    [
        [],  # no entry
        garments((1, "neck", "hips")),  # not on the scale
        garments((1, "knees", "waist")),  # ends above its start
        garments((True, "shoulders", "hips")),  # no group
        [{"g": 1, "starts": "shoulders"}],  # no end
        ["not an entry"],
    ],
)
def test_a_garment_without_a_valid_span_is_framed_for_its_category(
    fake_gen, images, caplog, answer
):
    """No silent fallback: warned and reported (the result is not
    cached); the framing is ``FRAMING_BY_CATEGORY``'s."""
    fake_gen.json({"photos": [row("top", 1)], "garments": answer})
    with degraded.tracking() as marker:
        result = classify_products(images[:1])
    (top,) = result.products
    assert (top.span, result.framing) == (None, "upper_body")
    assert "classifier gave garment A no valid span" in caplog.text
    assert marker.reasons == [
        "classifier gave a garment no valid start and end"
    ]
