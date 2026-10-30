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

import asyncio
import io
from types import SimpleNamespace

import numpy as np
from PIL import Image

from genmedia4commerce.core import degraded
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows import details

PHOTOS = [png(1000, 800), png(1000, 800)]
BIG = [png(2100, 3300)]  # a product photo: small details still count


def item(kind, text, box, side="front", photo=0, legible=True, where="plaque"):
    return {
        "kind": kind,
        "text": text,
        "legible": legible,
        "photo": photo,
        "box_2d": list(box),
        "side": side,
        "where": where,
    }


def size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def test_outside_details_lettering_first_cropped_with_context(fake_gen):
    fake_gen.json(
        {
            "details": [
                item("hardware", "", (100, 100, 200, 200), where="buckle"),
                item("text", "Aurelia", (700, 400, 760, 600)),
                item("label", "Aurelia", (300, 300, 500, 500), side="inside"),
            ]
        }
    )
    found = details.find(PHOTOS)
    assert [(d.kind, d.text, d.where) for d in found] == [
        ("text", "Aurelia", "plaque"),
        ("hardware", "", "buckle"),
    ]
    # 200 x 48 px plaque: 2.75 x the box, at least 256 px a side
    assert size(found[0].image) == (550, 256)
    call = next(c for c in fake_gen.calls if c["fn"] == "generate_json")
    assert call["params"].model == get_models().judge
    assert call["raw_parts"][:2] == ["IMAGE_0:", PHOTOS[0]]


def test_duplicates_dropped_tiny_details_skipped_neighbours_apart(fake_gen):
    fake_gen.json(
        {
            "details": [
                item("text", "Aurelia", (100, 100, 200, 400)),
                item("text", "MADE IN ITALY", (180, 150, 260, 350)),
                item("hardware", "", (500, 100, 600, 200), where="buckle"),
                item("hardware", "", (500, 700, 600, 800), where="buckle"),
                item("text", "Aurelia", (100, 100, 200, 400), photo=1),
                item("logo", "", (900, 900, 930, 930)),  # 30 px: too small
            ]
        }
    )
    found = details.find(PHOTOS, limit=5)
    assert [(d.kind, d.text) for d in found] == [
        ("text", "Aurelia"),
        ("text", "MADE IN ITALY"),  # overlapping, never merged
        ("hardware", ""),
    ]
    assert found[0].box == (100, 100, 200, 400)


def test_illegible_text_is_not_quoted_and_the_limit_holds(fake_gen):
    fake_gen.json(
        {
            "details": [
                item("text", "Fenari", (100, 100, 200, 300), legible=False),
                item("logo", "", (400, 400, 500, 500), where="emblem"),
            ]
        }
    )
    [only] = details.find(PHOTOS, limit=1)
    assert (only.kind, only.text) == ("text", "")


def test_a_failed_search_means_no_closeups(fake_gen):
    fake_gen.json(RuntimeError("judge down"))
    assert details.find(PHOTOS) == []
    assert details.find(PHOTOS, limit=0) == []


def _patterned() -> bytes:
    """A photo with a striped emblem at (100..200, 100..200) and the same
    emblem at (100..200, 700..800), and a checked one at (500..600, 100..200)."""
    arr = np.full((800, 1000, 3), 240, np.uint8)
    stripes = np.tile(np.repeat([0, 255], 10), 5)[:, None]  # 100 rows
    for x0 in (100, 700):
        arr[80:160, x0 : x0 + 100] = stripes[:80, :, None]
    checks = (np.indices((80, 100)).sum(axis=0) // 10 % 2 * 255)[..., None]
    arr[400:480, 100:200] = checks
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_a_logo_found_twice_under_different_sides_is_kept_once(fake_gen):
    photo = _patterned()
    fake_gen.json(
        {
            "details": [
                item("logo", "", (100, 100, 200, 200), where="emblem"),
                item("logo", "", (100, 700, 200, 800), side="left", where="x"),
            ]
        }
    )
    assert [d.where for d in details.find([photo], limit=5)] == ["emblem"]


def test_two_different_logos_on_one_side_are_both_kept(fake_gen):
    photo = _patterned()
    fake_gen.json(
        {
            "details": [
                item("logo", "", (100, 100, 200, 200), where="stripes"),
                item("logo", "", (500, 100, 600, 200), where="checks"),
            ]
        }
    )
    found = details.find([photo], limit=5)
    assert [d.where for d in found] == ["stripes", "checks"]


def test_a_logo_inside_the_padlock_is_the_padlock_the_stamp_stays_apart(
    fake_gen,
):
    # MO Look 1's bag: merged with the strap stamp below it, the padlock's
    # box doubled in height and its close-up showed mostly strap.
    fake_gen.json(
        {
            "details": [
                item("hardware", "", (401, 161, 517, 263), where="padlock"),
                item("logo", "MO", (459, 173, 496, 211), where="disc"),
                item("text", "MO", (592, 167, 608, 210), legible=False),
            ]
        }
    )
    found = details.find(BIG, limit=5, upscale=False)
    assert [(d.where, d.text, d.box) for d in found] == [
        ("padlock", "MO", (401, 161, 517, 263)),
        ("plaque", "", (592, 167, 608, 210)),
    ]


def test_a_lettered_button_and_a_plain_one_are_two_details(fake_gen):
    # MO Look 1's blazer: the plain bottom button was dropped as a duplicate
    # of the MO top one, and the drawing put the logo on both.
    fake_gen.json(
        {
            "details": [
                item("hardware", "MO", (551, 476, 583, 517), where="top"),
                item("hardware", "", (699, 478, 729, 518), where="bottom"),
            ]
        }
    )
    found = details.find(BIG, limit=5, upscale=False)
    assert [d.where for d in found] == ["top", "bottom"]


def test_the_search_asks_one_box_per_detail_and_the_whole_button_row():
    assert (
        "One box per detail: a label or patch with several lines of text is "
        "one box; the buttons, snaps, studs or rivets of one closure are one "
        "box around the whole set, from the first to the last (it shows "
        "their count, spacing and where the garment ends). Never box a pocket "
        "set, a panel or most of the garment."
    ) in details._ASK
    assert "short run" not in details._ASK


def test_a_far_neighbour_is_not_merged_into_the_lettering(fake_gen):
    # The tote: a small stud near the "Aurelia" patch stays a separate detail
    # (merged, the close-up would be the whole top of the bag).
    fake_gen.json(
        {
            "details": [
                item("text", "Aurelia", (500, 400, 560, 560), where="patch"),
                item("hardware", "", (360, 280, 420, 340), where="stud"),
            ]
        }
    )
    found = details.find(PHOTOS)
    assert [(d.where, d.box) for d in found] == [
        ("patch", (500, 400, 560, 560)),
        ("stud", (360, 280, 420, 340)),
    ]


def test_without_upscale_the_closeups_stay_crops(fake_gen):
    fake_gen.json({"details": [item("text", "Aurelia", (700, 400, 760, 600))]})
    [plaque] = details.find(PHOTOS, upscale=False)
    assert size(plaque.image) == (550, 256)  # the 2.75x crop itself
    assert [c["fn"] for c in fake_gen.calls] == ["generate_json"]


def test_each_closeup_is_upscaled_with_its_lettering_quoted(fake_gen):
    from genmedia4commerce.core import media

    fake_gen.json(
        {
            "details": [
                item("text", "Aurelia", (700, 400, 760, 600)),
                item("logo", "", (100, 100, 200, 200), where="emblem"),
            ]
        }
    )
    fake_gen.image(b"UPSCALED")
    found = details.find(PHOTOS, upscale=False)
    plaque, emblem = details.upscale(found)
    assert (plaque.image, emblem.image) == (b"UPSCALED", b"UPSCALED")
    assert (plaque.text, emblem.where) == ("Aurelia", "emblem")
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_image"]
    assert sorted(c["raw_parts"][1] for c in calls) == sorted(
        d.image for d in found
    )
    prompts = sorted(c["raw_parts"][0] for c in calls)
    assert prompts[0] == media.UPSCALE_PROMPT  # the emblem: no lettering
    assert "reads exactly: 'Aurelia'" in prompts[1]
    assert {c["params"].image_size for c in calls} == {"2K"}


def test_find_upscales_by_default(fake_gen):  # as the 360 spin uses it
    fake_gen.json({"details": [item("text", "Aurelia", (700, 400, 760, 600))]})
    fake_gen.image(b"UPSCALED")
    [plaque] = details.find(PHOTOS)
    assert plaque.image == b"UPSCALED"


def test_a_failed_upscale_keeps_the_crop(fake_gen):
    fake_gen.json({"details": [item("text", "Aurelia", (700, 400, 760, 600))]})
    fake_gen.image(RuntimeError("image model down"))
    [crop] = details.find(PHOTOS, upscale=False)
    assert details.upscale([crop]) == [crop]
    [plaque] = details.find(PHOTOS)
    assert size(plaque.image) == (550, 256)  # the 2.75x crop itself
    assert details.upscale([]) == []


def test_find_each_searches_each_photo_alone_without_upscaling(fake_gen):
    plain, lettered = png(1000, 800, (1, 2, 3)), png(1000, 800, (4, 5, 6))

    def respond(parts, **_kwargs):
        assert len(parts) == 3 and parts[0] == "IMAGE_0:"  # one photo
        if parts[1] == lettered:
            return {"details": [item("text", "Aurelia", (700, 400, 760, 600))]}
        return {"details": []}

    fake_gen.json(respond)
    found = details.find_each([plain, lettered, plain], limit=1)
    assert [[d.text for d in hits] for hits in found] == [[], ["Aurelia"], []]
    assert size(found[1][0].image) == (550, 256)  # the crop itself
    assert [c["fn"] for c in fake_gen.calls] == ["generate_json"] * 3
    assert details.find_each([]) == []


def test_find_each_gives_a_failed_photo_no_closeups(monkeypatch):
    def find(images, *, limit, upscale, product):
        if images == [b"broken"]:
            raise ValueError("cannot open the photo")
        return [(images, limit, upscale, product)]

    monkeypatch.setattr(details, "find", find)
    assert details.find_each([b"ok", b"broken"], limit=1) == [
        [([b"ok"], 1, False, "")],
        [],
    ]


def test_each_photo_is_searched_told_its_products_name(fake_gen):
    skirt, jacket = png(1000, 800, (1, 2, 3)), png(1000, 800, (4, 5, 6))
    asks = {}

    def respond(parts, **_kwargs):
        asks[parts[1]] = parts[2]
        return {  # two kinds: flat boxes of one kind would be duplicates
            "details": [
                item("hardware", "", (100, 100, 200, 300)),
                item("logo", "", (600, 400, 700, 600)),
            ]
        }

    fake_gen.json(respond)
    found = details.find_each(
        [skirt, jacket], limit=2, products=["pencil skirt", "field jacket"]
    )
    assert [len(hits) for hits in found] == [2, 2]  # the limit for each
    assert asks[skirt] == details._ASK + (
        " The product is the pencil skirt: return only details on it, never "
        "on other garments, shoes, accessories or the person."
    )
    assert asks[jacket].endswith(
        " The product is the field jacket: return only details on it, never "
        "on other garments, shoes, accessories or the person."
    )


def test_an_anchor_goes_first_and_only_details_visible_on_it_are_asked(
    fake_gen,
):
    """The try-on's anchor is IMAGE_0, the other photos follow; the ask
    gains one sentence, only with an anchor; a detail is cropped from the
    photo it names, the anchor counted as 0."""
    anchor = png(1000, 800, (10, 200, 10))
    asks = []

    def respond(parts, **_kwargs):
        asks.append(parts)
        return {
            "details": [
                item("logo", "", (100, 100, 300, 300), photo=0),
                item("hardware", "", (500, 500, 700, 700), photo=2),
            ]
        }

    fake_gen.json(respond)
    found = details.find(PHOTOS, upscale=False, product="jacket", anchor=anchor)
    assert asks[0][:6] == [
        "IMAGE_0:",
        anchor,
        "IMAGE_1:",
        PHOTOS[0],
        "IMAGE_2:",
        PHOTOS[1],
    ]
    named = details._ASK + details._PRODUCT.format(product="jacket")
    assert asks[0][-1] == named + (
        " IMAGE_0 shows the product as it will be drawn: return only details "
        "visible on the product in IMAGE_0, each from the photo where it is "
        "largest and sharpest (another photo may show the same detail "
        "closer)."
    )
    assert [d.photo for d in found] == [0, 2]
    pixel = Image.open(io.BytesIO(found[0].image)).getpixel((5, 5))
    assert all(
        abs(a - b) < 8 for a, b in zip(pixel, (10, 200, 10), strict=True)
    )
    details.find(PHOTOS, upscale=False, product="jacket")  # no anchor
    assert asks[1][:2] == ["IMAGE_0:", PHOTOS[0]]
    assert asks[1][-1] == named


def test_the_video_try_on_names_its_product_the_spin_none(fake_gen):
    """The video try-on's search is told its product's name; the spin's (a
    single product) asks the base question alone, as before; neither has
    an anchor."""
    from genmedia4commerce.workflows.spin import pipeline as spin
    from genmedia4commerce.workflows.video_tryon import refs

    asks = []

    def respond(parts, **_kwargs):
        asks.append(parts[-1])
        return {"details": []}

    fake_gen.json(respond)
    assert refs.find_details(PHOTOS[:1], ["tote"]) == [[]]
    spin_req = SimpleNamespace(product_images=PHOTOS)
    assert asyncio.run(spin._closeups(spin_req, shoes=False)) == []
    assert asks == [
        details._ASK + details._PRODUCT.format(product="tote"),
        details._ASK,
    ]
    assert "The product is" not in details._ASK


def test_each_detail_is_named_by_what_it_is_not_where(fake_gen):
    zip_pull = item("hardware", "", (100, 100, 300, 300), where="top of seam")
    fake_gen.json({"details": [{**zip_pull, "name": " side zip "}]})
    (found,) = details.find(PHOTOS, upscale=False)
    assert (found.name, found.where) == ("side zip", "top of seam")
    assert (
        "its name: one to three words for the object itself, never its "
        "place ('side zip', 'padlock', 'front buttons', 'brand plaque')"
    ) in details._ASK
    assert (
        "name" in details._SCHEMA["properties"]["details"]["items"]["required"]
    )


def test_a_close_up_upscale_that_keeps_the_crop_is_reported(fake_gen):
    fake_gen.image(GenerationError("image model down"))
    crop = details.Detail("text", "MO", "front", "stamp", png(9, 9))
    with degraded.tracking() as marker:
        (kept,) = details.upscale([crop])
    assert kept is crop
    assert marker.reasons == ["close-up upscale"]
