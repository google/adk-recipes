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

"""Tests for the box calls, the products the look adds and the heroes."""

import asyncio
import io
import threading
import time

import pytest
from PIL import Image

from genmedia4commerce.core import degraded, media
from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.workflows.tryon import cut, hero, outfit, reference
from genmedia4commerce.workflows.tryon.cut import Boxed


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


@pytest.fixture
def model_photo() -> bytes:
    return png(1000, 2000, color=(120, 120, 120))


# ---- the box call ----------------------------------------------------------

BANDS = {"upper_body": [100, 0, 500, 1000], "lower_body": [450, 0, 1000, 1000]}


def test_bands_from_the_detector_boxes(fake_gen, model_photo):
    fake_gen.json(BANDS)
    boxed = cut.box_items(model_photo, bands=True)
    assert list(boxed.bands) == ["top", "bottom"]  # no waist band
    assert _size(boxed.bands["top"]) == (1000, 800)
    assert _size(boxed.bands["bottom"]) == (1000, 1100)
    assert (boxed.crops, boxed.boxes) == ([], [])
    [call] = fake_gen.calls
    assert call["raw_parts"][0] == media.to_png(model_photo)
    prompt = call["raw_parts"][1]
    assert '"items"' not in prompt
    assert '"waist"' not in prompt
    assert call["params"].model == get_models().judge
    assert call["params"].thinking == "MEDIUM"


def test_a_failed_detector_is_asked_again_once(fake_gen, model_photo):
    fake_gen.json([GenerationError("detector down"), BANDS])
    boxed = cut.box_items(model_photo, bands=True)
    assert list(boxed.bands) == ["top", "bottom"]
    assert len(fake_gen.calls) == 2


def test_invalid_boxes_twice_give_no_bands(fake_gen, model_photo):
    fake_gen.json({"upper_body": [500, 0, 100, 1000], "lower_body": None})
    with degraded.tracking() as marker:
        assert cut.box_items(model_photo, bands=True).bands == {}
    assert len(fake_gen.calls) == cut.DETECT_TRIES
    # A weak answer: the preprocessing cache must not keep it.
    assert marker.reasons == ["look photo cut without its halves"]


def test_an_empty_lower_body_is_no_bottom_half_and_no_retry(
    fake_gen, model_photo
):
    """Bare legs between the hem and the shoes (a mini skirt, shorts)."""
    fake_gen.json({"upper_body": BANDS["upper_body"], "lower_body": []})
    with degraded.tracking() as marker:
        boxed = cut.box_items(model_photo, bands=True)
    assert marker.ok  # a real answer, not a fallback
    assert list(boxed.bands) == ["top"]
    assert _size(boxed.bands["top"]) == (1000, 800)
    assert len(fake_gen.calls) == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"upper_body": BANDS["upper_body"], "lower_body": [450, 0]},
        {"upper_body": [], "lower_body": []},
    ],
    ids=["malformed-lower", "invalid-upper"],
)
def test_other_invalid_bands_are_asked_again(fake_gen, model_photo, answer):
    fake_gen.json(answer)
    assert cut.box_items(model_photo, bands=True).bands == {}
    assert len(fake_gen.calls) == cut.DETECT_TRIES


def test_the_bottom_half_is_asked_only_when_the_hem_reaches_the_shoes(
    fake_gen, model_photo
):
    fake_gen.json(BANDS)
    cut.box_items(model_photo, bands=True)
    [call] = fake_gen.calls
    assert (
        "Return it only when the hem of the lowest garment on the legs (the "
        "bottom, a dress or a long coat) reaches the shoes or boots, with at "
        "most a hand's width of bare leg between them; tall boots count as "
        "shoes (a mini skirt over knee boots reaches them). Otherwise return "
        '"lower_body": [].'
    ) in call["raw_parts"][1]
    assert "unless" not in call["raw_parts"][1]
    assert "lower_body" in call["schema"]["required"]
    assert "minItems" not in call["schema"]["properties"]["lower_body"]


def test_no_answer_at_all_is_a_retryable_error_not_lost_products(
    fake_gen, model_photo
):
    fake_gen.json(GenerationError("unparsable JSON"))
    with pytest.raises(GenerationError, match="could not be cut") as err:
        cut.box_items(model_photo, [("top", "field jacket")], bands=True)
    assert err.value.retryable
    assert len(fake_gen.calls) == cut.DETECT_TRIES


def test_the_box_call_asks_for_a_fixed_json_shape(fake_gen, model_photo):
    fake_gen.json({**BANDS, "items": []})
    cut.box_items(model_photo, bands=True)
    cut.box_items(model_photo, [("top", "field jacket")], bands=True)
    cut.box_items(model_photo, [("top", "field jacket")])
    bands, both, items = (c["schema"] for c in fake_gen.calls)
    assert bands["required"] == ["upper_body", "lower_body"]
    assert both["required"] == ["upper_body", "lower_body", "items"]
    assert items["required"] == ["items"]
    entry = items["properties"]["items"]["items"]
    assert entry["required"] == ["index", "box"]


ASKED = [("bottom", "midi skirt"), ("footwear", "lace-up shoes")]


def test_the_box_call_is_told_each_product_by_its_number(fake_gen, model_photo):
    fake_gen.json({"items": []})
    cut.box_items(model_photo, ASKED)
    prompt = fake_gen.calls[0]["raw_parts"][1]
    assert prompt.startswith("You see a fashion photo. Return boxes as")
    assert '0: bottom "midi skirt"\n1: footwear "lace-up shoes"\n' in prompt
    assert '"box": [] when the product is not in the photo.' in prompt
    assert "upper_body" not in prompt
    assert "Already supplied" not in prompt  # it decides nothing new


def test_each_product_is_cut_by_its_index_with_context(fake_gen, model_photo):
    fake_gen.json(
        {
            "items": [
                {"index": 1, "box": [900, 300, 1000, 700]},
                {"index": 0, "box": [450, 200, 750, 800]},
                {"index": 0, "box": [0, 0, 100, 100]},  # the first one wins
                {"index": 7, "box": [0, 0, 500, 500]},  # not asked
            ]
        }
    )
    boxed = cut.box_items(model_photo, ASKED)
    skirt, shoes = boxed.crops
    assert _size(skirt) == (636, 636)  # 600 x 600 px + 3% each side
    assert _size(shoes) == (424, 206)  # clamped at the bottom edge
    assert boxed.boxes == [(200, 900, 800, 1500), (300, 1800, 700, 2000)]
    assert boxed.bands == {}


def test_a_product_not_found_gets_no_crop_never_another(
    fake_gen, model_photo, caplog
):
    fake_gen.json(
        {"items": [{"index": 0, "box": []}, {"index": 1, "box": [1]}]}
    )
    with degraded.tracking() as marker:
        boxed = cut.box_items(model_photo, ASKED)
    assert boxed.crops == [None, None]
    assert marker.reasons == ["box call missed a product"] * 2
    assert "no box for bottom 'midi skirt'" in caplog.text


def test_a_tiny_product_gets_no_crop(fake_gen, model_photo):
    fake_gen.json({"items": [{"index": 0, "box": [500, 100, 510, 110]}]})
    with degraded.tracking() as marker:
        boxed = cut.box_items(model_photo, [("jewelry_hand", "ring")])
    assert boxed.crops == [None] and boxed.boxes == [(100, 1000, 110, 1020)]
    assert marker.ok  # found, too small to show anything


def test_items_survive_invalid_bands(fake_gen, model_photo):
    items = [{"index": 0, "box": [450, 200, 750, 800]}]
    fake_gen.json({**BANDS, "upper_body": [], "items": items})  # legs only
    boxed = cut.box_items(model_photo, ASKED[:1], bands=True)
    assert boxed.bands == {}
    assert boxed.crops[0] is not None


HALF = [153, 241, 515, 906]  # the top half, chest to below the waist
POLO = {"index": 1, "box": [155, 243, 513, 904]}
COAT = {"index": 2, "box": [150, 230, 800, 920]}  # IoU 0.54 with the half
SHOE = {"index": 0, "box": [900, 300, 1000, 700]}
VEST = {"index": 3, "box": [160, 250, 500, 890]}  # inside the polo's box
UPPER = [
    ("footwear", "lace-up shoes"),
    ("top", "polo shirt"),
    ("full_body_outer", "long coat"),
    ("top", "vest"),
]


def _box_upper(fake_gen, model_photo, *items):
    answer = {"upper_body": HALF, "lower_body": [], "items": [*items]}
    fake_gen.json(answer)
    return cut.box_items(model_photo, UPPER, bands=True)


def test_an_upper_product_boxed_like_the_top_half_is_found(
    fake_gen, model_photo
):
    boxed = _box_upper(fake_gen, model_photo, SHOE, POLO)
    assert boxed.top_half == [1]  # the polo: the same picture twice
    assert _size(boxed.crops[1]) == (701, 758)  # its crop stays its own


def test_a_long_coat_keeps_its_crop_and_the_half(fake_gen, model_photo):
    boxed = _box_upper(fake_gen, model_photo, COAT)
    assert boxed.top_half == []
    assert _size(boxed.crops[2]) == (732, 1378)  # its own box + 3% each side


def test_a_coat_a_little_longer_than_the_half_keeps_its_crop(
    fake_gen, model_photo
):
    """It overlaps the half by 0.8 but ends below it (a knee-length coat,
    UC612S): the half would cut it off."""
    coat = {"index": 2, "box": [HALF[0], HALF[1], HALF[2] + 85, HALF[3]]}
    boxed = _box_upper(fake_gen, model_photo, coat)
    assert boxed.top_half == []


def test_upper_products_boxed_like_the_half_come_largest_first(
    fake_gen, model_photo
):
    """A larger box that is not like the half (the coat) does not count."""
    boxed = _box_upper(fake_gen, model_photo, VEST, COAT, POLO)
    assert boxed.top_half == [1, 3]


LOW_HALF = [452, 300, 1000, 640]  # the bottom half, the waist to the feet
JEANS = {"index": 1, "box": [460, 310, 960, 630]}  # IoU 0.86 with the half
SKIRT_BOX = {"index": 2, "box": [470, 320, 950, 620]}  # inside the jeans'
LOWER = [
    ("footwear", "lace-up shoes"),
    ("bottom", "jeans"),
    ("bottom", "maxi skirt"),
    ("dress", "maxi dress"),
    ("full_body_outer", "long coat"),
]


def _box_lower(fake_gen, model_photo, *items, lower=LOW_HALF):
    answer = {"upper_body": HALF, "lower_body": lower, "items": [*items]}
    fake_gen.json(answer)
    return cut.box_items(model_photo, LOWER, bands=True)


def test_a_bottom_boxed_like_the_bottom_half_is_found(fake_gen, model_photo):
    boxed = _box_lower(fake_gen, model_photo, SHOE, JEANS)
    assert boxed.bottom_half == [1]  # the jeans: the same picture twice
    assert boxed.top_half == []
    assert _size(boxed.crops[1]) == (340, 1060)  # its crop stays its own


def test_a_bottom_much_smaller_than_the_bottom_half_is_not_it(
    fake_gen, model_photo
):
    """Inside the half, but it overlaps it by 0.41 only."""
    skirt = {"index": 2, "box": [460, 310, 700, 630]}
    assert _box_lower(fake_gen, model_photo, skirt).bottom_half == []


def test_a_bottom_a_little_taller_than_the_half_keeps_its_crop(
    fake_gen, model_photo
):
    """It overlaps the half by 0.9 but starts above it (high-waisted): the
    half would cut it off."""
    jeans = {"index": 1, "box": [LOW_HALF[0] - 60, *LOW_HALF[1:]]}
    assert _box_lower(fake_gen, model_photo, jeans).bottom_half == []


@pytest.mark.parametrize("index", [3, 4], ids=["dress", "long-coat"])
def test_a_dress_or_long_coat_is_never_the_bottom_half(
    fake_gen, model_photo, index
):
    """It runs past the half: boxed exactly like it, it keeps its crop."""
    item = {"index": index, "box": LOW_HALF}
    boxed = _box_lower(fake_gen, model_photo, item)
    assert (boxed.bottom_half, boxed.top_half) == ([], [])
    assert boxed.crops[index] is not None


def test_bottoms_boxed_like_the_bottom_half_come_largest_first(
    fake_gen, model_photo
):
    """A larger box of another category (the dress) does not count."""
    dress = {"index": 3, "box": LOW_HALF}
    boxed = _box_lower(fake_gen, model_photo, SKIRT_BOX, dress, JEANS)
    assert boxed.bottom_half == [1, 2]


def test_no_bottom_half_no_bottom_like_it(fake_gen, model_photo):
    """The hem above the shoes: no bottom half, so nothing is boxed like
    it; the jeans keep their crop."""
    boxed = _box_lower(fake_gen, model_photo, JEANS, lower=[])
    assert list(boxed.bands) == ["top"]
    assert boxed.bottom_half == []
    assert boxed.crops[1] is not None


def test_upscale_shoes(fake_gen):
    fake_gen.image(b"SHOES-2K")
    assert reference.upscale_shoes(png(10, 5)) == b"SHOES-2K"
    [call] = fake_gen.calls
    assert call["raw_parts"][0] == media.UPSCALE_PROMPT
    assert call["params"].image_size == "2K"


def test_a_failed_shoe_upscale_keeps_the_crop(fake_gen):
    fake_gen.image(GenerationError("image model down"))
    crop = png(10, 5)
    assert reference.upscale_shoes(crop) == crop


def test_bands_go_to_the_products_of_their_categories():
    categories = ("top", "full_body_outer", "dress", "bottom", "bag")
    products = [
        Product(chr(65 + i), c, f"{c} item", [ProductRef(png(), "front")])
        for i, c in enumerate(categories)
    ]
    c = Classification("outfit", products, "full_body")
    reference.attach_reference_bands(c, {"top": b"TOP", "bottom": b"LOW"})
    crops = {
        p.category: [(r.image, r.view, r.presentation) for r in p.refs[1:]]
        for p in c.products
    }
    assert crops == {
        "top": [(b"TOP", "front", "worn")],
        # a dress or long coat gets both halves
        "full_body_outer": [
            (b"TOP", "front", "worn"),
            (b"LOW", "front", "worn"),
        ],
        "dress": [(b"TOP", "front", "worn"), (b"LOW", "front", "worn")],
        "bottom": [(b"LOW", "front", "worn")],
        "bag": [],  # no band shows it
    }
    assert all(r.source == "model_crop" for p in c.products for r in p.refs[1:])


# ---- the products, their crops and heroes ----------------------------------

PHOTOS = [png(color=(20 + 20 * i, 40, 60)) for i in range(12)]
MODEL = png(color=(9, 9, 9))
TOP, LOW = b"TOP-HALF", b"LOW-HALF"


def _crop(k: int, w: int = 500, h: int = 600) -> bytes:
    """A crop, large enough to be a hero unless ``w`` and ``h`` say not."""
    return png(w, h, color=(200, 10 * k, 10))


CROP, SCARF, SHOES, POLO_IMG = _crop(1), _crop(2), _crop(3), _crop(4)
SKIRT, BAG, OWN_SHOES, VEST_IMG = _crop(5), _crop(6), _crop(7), _crop(8)
JEANS_IMG = _crop(20)
TINY = _crop(9, 399, 150)  # under MIN_HERO_SIDE
UPSCALED = b"SHOES-2K"


def row(c, g, s="front", w=False, x=False, items=(), d=None):
    """A classifier row; ``items``: (category, name, group[, covered]) shown
    whole."""
    shown = [
        {"c": ic, "d": idn, "g": ig, "covered": bool(rest and rest[0])}
        for ic, idn, ig, *rest in items
    ]
    d = d or f"{c} item"
    return {"c": c, "d": d, "s": s, "w": w, "x": x, "g": g, "items": shown}


def look(*items, s="front"):
    """A full-body look photo's row (or the reference model's)."""
    return row("full_body_look", -1, s, True, False, items, "look")


def own(c, g):
    """The item of the uploaded product of group ``g``."""
    return (c, f"{c} item", g)


def _photo(image: bytes) -> int | str:
    """The index of ``image`` in ``PHOTOS``; "model" for the reference
    model."""
    index = {id(p): i for i, p in enumerate(PHOTOS)} | {id(MODEL): "model"}
    return index[id(image)]


class Boxes:
    """``reference.box_items``, recorded: the halves when asked, and the
    crop ``crops`` names for each product asked (None: not found), or, for
    a dict, the crop it names for the photo boxed."""

    def __init__(
        self, monkeypatch, crops=None, *, half=None, low=None, delay=0.0
    ):
        self.calls: list[tuple] = []
        self.crops, self.half, self.delay = crops or {}, half, delay
        self.low = low
        self.running, self.most = 0, 0
        lock = threading.Lock()

        def box_items(image, items=(), *, bands=False):
            with lock:
                self.calls.append((image, list(items), bands))
                self.running += 1
                self.most = max(self.most, self.running)
            time.sleep(self.delay)
            with lock:
                self.running -= 1
            crops = [self._crop(image, name) for _, name in items]
            names = [name for _, name in items]
            top = [names.index(h) for h in self.half or [] if h in names]
            bottom = [names.index(h) for h in self.low or [] if h in names]
            halves = {"top": TOP, "bottom": LOW} if bands else {}
            return Boxed(halves, crops, [None] * len(items), top, bottom)

        monkeypatch.setattr(reference, "box_items", box_items)

    def _crop(self, image: bytes, name: str) -> bytes | None:
        crop = self.crops.get(name)
        return crop.get(_photo(image)) if isinstance(crop, dict) else crop

    def asked(self) -> list[tuple]:
        """(photo index, product names asked, halves asked) per call; the
        reference model's index is "model"."""
        return [
            (_photo(image), [n for _, n in items], bands)
            for image, items, bands in self.calls
        ]


def _run(fake_gen, rows, *, model=None, complete_look=True, **kwargs):
    """``reference.classify`` of ``len(rows)`` photos (the reference model
    last with ``model``) on the classifier's ``rows``."""
    fake_gen.json({"photos": rows})
    n = len(rows) - (model is not None)
    return asyncio.run(
        reference.classify(PHOTOS[:n], model, complete_look, **kwargs)
    )


def _heroes(c: Classification) -> list[tuple]:
    """(id, category, name, hero image, its source) of each product."""
    return [
        (p.id, p.category, p.description, p.refs[0].image, p.refs[0].source)
        for p in c.products
    ]


def test_packshots_alone_are_their_own_heroes_with_no_box_call(
    fake_gen, monkeypatch
):
    """Each packshot lists its own product: never a crop source of it."""
    boxes = Boxes(monkeypatch)
    rows = [row("top", 1, items=[own("top", 1)]), row("bag", 2, "other")]
    c, crops = _run(fake_gen, rows)
    assert boxes.calls == [] and crops == {}
    assert [r[3] for r in _heroes(c)] == PHOTOS[:2]  # flat, fallback


def test_a_whole_worn_front_upload_beats_a_crop_and_is_never_boxed(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"top item": CROP})
    rows = [
        row("top", 1, items=[own("top", 1)]),  # flat
        row("top", 1, w=True, items=[own("top", 1)]),  # worn: the hero
        row("top", 1, w=True, x=True),  # a zoom photo: never the hero
        look(own("top", 1)),
    ]
    c, _ = _run(fake_gen, rows)
    assert boxes.asked() == [(3, [], True)]  # the halves only
    (top,) = c.products
    assert top.refs[0].image is PHOTOS[1]
    assert [r.image for r in top.refs] == [PHOTOS[1], PHOTOS[0], TOP]


def test_a_crop_beats_a_flat_photo_and_the_other_photos_stay(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"bottom item": CROP})
    rows = [
        row("bottom", 1, items=[own("bottom", 1)]),
        row("bottom", 1, "back", items=[own("bottom", 1)]),
        look(own("bottom", 1)),
    ]
    c, crops = _run(fake_gen, rows)
    assert boxes.asked() == [(2, ["bottom item"], True)]
    (skirt,) = c.products
    assert [(r.image, r.view, r.source) for r in skirt.refs] == [
        (CROP, "front", "model_crop"),  # the hero
        (PHOTOS[0], "front", "upload"),
        (PHOTOS[1], "back", "upload"),
        (LOW, "front", "model_crop"),
    ]
    assert skirt.refs[0].presentation == "worn"  # cut from a worn photo
    assert crops == {"top": TOP, "bottom": LOW, "item-0": CROP}


def test_a_product_not_boxed_goes_on_to_its_flat_photo_then_the_fallback(
    fake_gen, monkeypatch
):
    """Never a silent wrong crop: the next tier."""
    boxes = Boxes(monkeypatch)  # finds nothing
    rows = [
        row("bottom", 1, items=[own("bottom", 1)]),  # flat front
        row("bag", 2, "other", items=[own("bag", 2)]),  # a side view only
        look(own("bottom", 1), own("bag", 2)),
    ]
    c, crops = _run(fake_gen, rows)
    assert boxes.asked() == [(2, ["bottom item", "bag item"], True)]
    assert [r[3] for r in _heroes(c)] == PHOTOS[:2]
    assert set(crops) == {"top", "bottom"}


def test_crops_come_only_from_photos_facing_front(fake_gen, monkeypatch):
    """The look photo first, then the other front photos; never a product's
    own flat photo, a turned or a back photo."""
    boxes = Boxes(monkeypatch)
    rows = [
        row("top", 1, items=[own("top", 1), own("bottom", 2)]),
        row("bottom", 2, "other", w=True, items=[own("bottom", 2)]),
        row("bottom", 2, "back", w=True, items=[own("bottom", 2)]),
        row("footwear", 3, items=[own("footwear", 3)]),
        look(own("footwear", 3), s="other"),  # turned: halves only
    ]
    _run(fake_gen, rows)
    # the trousers from the top's photo; the top and shoes: their packshots
    assert sorted(boxes.asked()) == [(0, ["bottom item"], False), (4, [], True)]


def test_every_front_photo_showing_it_is_boxed_and_the_largest_crop_wins(
    fake_gen, monkeypatch
):
    """The trousers show whole in the look photo and in the top's photo:
    both are boxed, and the larger crop is the hero."""
    small = _crop(10, 450, 500)
    boxes = Boxes(monkeypatch, {"bottom item": {2: small, 0: CROP}})
    rows = [
        row("top", 1, items=[own("top", 1), own("bottom", 2)]),
        row("bottom", 2, items=[own("bottom", 2)]),
        look(own("bottom", 2)),
    ]
    c, crops = _run(fake_gen, rows)
    assert sorted(boxes.asked()) == [
        (0, ["bottom item"], False),
        (2, ["bottom item"], True),
    ]
    _, trousers = c.products
    assert trousers.refs[0].image is CROP
    assert trousers.refs[0].presentation == "flat"  # cut from a packshot
    assert crops == {"top": TOP, "bottom": LOW, "item-0": CROP}


def test_an_added_bag_edge_on_in_the_look_is_cut_from_a_larger_photo(
    fake_gen, monkeypatch
):
    """P3T72H: the bag hangs edge-on in the look photo (a thin strip) and
    shows from the front, larger, in the dress's worn photo. One call per
    photo, told all its products."""
    strip = _crop(10, 64, 452)
    crops = {"dress item": CROP, "heels": SHOES, "bag": {2: strip, 0: BAG}}
    boxes = Boxes(monkeypatch, crops)
    bag = ("bag", "bag", 2)
    rows = [
        row("dress", 1, w=True, items=[bag]),  # the dress not whole
        row("dress", 1, items=[own("dress", 1)]),
        look(own("dress", 1), ("footwear", "heels", 3), (*bag, True)),
    ]
    c, _ = _run(fake_gen, rows)
    assert sorted(boxes.asked()) == [
        (0, ["bag"], False),
        (2, ["dress item", "heels", "bag"], True),
    ]
    assert [(p[2], p[3]) for p in _heroes(c)] == [
        ("dress item", CROP),
        ("heels", SHOES),
        ("bag", BAG),
    ]
    assert c.products[2].refs[0].presentation == "worn"


def test_equal_crops_keep_the_look_photos_then_the_first_upload(
    fake_gen, monkeypatch
):
    """All 500 x 600 px. The second case asks photo 1 first (for the top),
    yet the trousers keep photo 0's crop."""
    bottom = own("bottom", 3)
    rows = [
        row("top", 1, items=[own("top", 1), bottom]),
        row("bag", 2, w=True, items=[own("bag", 2), own("top", 1), bottom]),
        row("bottom", 3, items=[bottom]),
    ]
    crops = {"bottom item": {0: CROP, 1: SCARF, 3: SHOES}, "top item": POLO_IMG}
    boxes = Boxes(monkeypatch, crops)
    c, _ = _run(fake_gen, [*rows, look(bottom)])
    assert sorted(boxes.asked()) == [
        (0, ["bottom item"], False),
        (1, ["top item", "bottom item"], False),
        (3, ["bottom item"], True),
    ]
    assert [p[3] for p in _heroes(c)] == [POLO_IMG, PHOTOS[1], SHOES]
    boxes.calls.clear()
    c, _ = _run(fake_gen, [*rows, look(s="back")])  # no look photo
    assert [p[3] for p in _heroes(c)] == [POLO_IMG, PHOTOS[1], CROP]


def test_one_box_call_per_photo_in_parallel_at_most_four(fake_gen, monkeypatch):
    """Six bottoms, each in the front photo of the top that follows it."""
    boxes = Boxes(monkeypatch, delay=0.2)
    rows = []
    for k in range(6):
        g = k + 1
        rows.append(row("bottom", g, "other", d=f"skirt {k}"))
    for k in range(6):
        shown = [("bottom", f"skirt {k}", k + 1)]
        rows.append(row("top", 7 + k, w=True, x=True, items=shown))
    _run(fake_gen, rows)
    assert sorted(boxes.asked()) == [
        (6 + k, [f"skirt {k}"], False) for k in range(6)
    ]
    assert boxes.most == reference.MAX_BOX_CALLS == 4


def test_the_look_adds_its_new_items_only(fake_gen, monkeypatch):
    """With a look photo, the new groups it lists (category and name of the
    item there); a new item only another photo shows is ignored."""
    boxes = Boxes(monkeypatch, {"neck scarf": SCARF, "loafers": SHOES})
    rows = [
        row("top", 1, w=True, items=[own("top", 1), ("bag", "tote", 5)]),
        look(
            own("top", 1),
            ("jewelry_neck", "neck scarf", 3),
            ("footwear", "loafers", 4),
            ("jewelry_neck", "neck scarf", 3),  # listed twice: once
        ),
    ]
    c, crops = _run(fake_gen, rows)
    assert boxes.asked() == [(1, ["neck scarf", "loafers"], True)]
    assert [(p[0], p[1], p[2], p[3]) for p in _heroes(c)] == [
        ("A", "top", "top item", PHOTOS[0]),
        ("B", "jewelry_neck", "neck scarf", SCARF),
        ("C", "footwear", "loafers", SHOES),
    ]
    assert (c.mode, c.framing) == ("outfit", "full_body")
    assert crops["item-0"] == SCARF and crops["item-1"] == SHOES


def test_without_a_look_photo_the_new_items_of_any_photo_join(
    fake_gen, monkeypatch
):
    """No front or turned full-body photo (a back one lists nothing)."""
    boxes = Boxes(monkeypatch, {"belt bag": BAG})
    rows = [
        row("top", 1, w=True, items=[own("top", 1), ("bag", "belt bag", 3)]),
        row("bottom", 2, items=[own("bottom", 2), ("bag", "belt bag", 3)]),
        look(s="back"),
    ]
    c, _ = _run(fake_gen, rows)
    assert sorted(boxes.asked()) == [  # every photo that shows it
        (0, ["belt bag"], False),
        (1, ["belt bag"], False),
    ]
    assert [p[2] for p in _heroes(c)] == ["top item", "bottom item", "belt bag"]


def test_a_turned_look_photo_crops_the_products_it_adds_only(
    fake_gen, monkeypatch
):
    """Their only source; an uploaded product stays front-only."""
    boxes = Boxes(monkeypatch, {"loafers": SHOES, "top item": CROP})
    rows = [
        row("top", 1, items=[own("top", 1)]),
        look(own("top", 1), ("footwear", "loafers", 2), s="other"),
    ]
    c, _ = _run(fake_gen, rows)
    assert boxes.asked() == [(1, ["loafers"], True)]
    assert [r[3] for r in _heroes(c)] == [PHOTOS[0], SHOES]


def test_an_uploaded_product_with_no_other_tier_is_cropped_from_a_turned_look(
    fake_gen, monkeypatch
):
    """Its only photo a close-up, no flat: the whole crop from the turned
    look photo beats falling back to the close-up (a coat, UMF470)."""
    boxes = Boxes(monkeypatch, {"full_body_outer item": CROP})
    rows = [
        row("top", 1, items=[own("top", 1)]),
        row("full_body_outer", 2, w=True, x=True),  # a close-up only
        look(own("top", 1), own("full_body_outer", 2), s="other"),
    ]
    c, _ = _run(fake_gen, rows)
    assert boxes.asked() == [(2, ["full_body_outer item"], True)]
    assert [r[3] for r in _heroes(c)] == [PHOTOS[0], CROP]


def test_without_complete_look_the_look_photo_still_gives_halves_and_crops(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"bottom item": CROP})
    rows = [
        row("bottom", 1, items=[own("bottom", 1)]),
        look(own("bottom", 1), ("footwear", "loafers", 2)),
    ]
    c, crops = _run(fake_gen, rows, complete_look=False)
    assert boxes.asked() == [(1, ["bottom item"], True)]  # no loafers
    (skirt,) = c.products
    assert [r.image for r in skirt.refs] == [CROP, PHOTOS[0], LOW]
    assert (c.mode, c.framing) == ("single", "lower_body")
    assert crops == {"top": TOP, "bottom": LOW, "item-0": CROP}


def test_the_reference_model_is_the_look_photo(fake_gen, monkeypatch):
    """Classified last, its row is never a product photo; the look photo
    among the uploads is then only another front photo."""
    boxes = Boxes(monkeypatch, {"loafers": SHOES})
    rows = [
        row("top", 1, items=[own("top", 1)]),
        look(own("top", 1)),
        row(
            "top", 1, w=True, items=[own("top", 1), ("footwear", "loafers", 2)]
        ),
    ]
    c, _ = _run(fake_gen, rows, model=MODEL)
    parts = fake_gen.calls[0]["raw_parts"]
    images = [PHOTOS[0], PHOTOS[1], MODEL]
    assert parts == [
        x for k, im in enumerate(images) for x in (f"IMAGE_{k}:", im)
    ]
    assert c.look_photo == 2 and c.photos[2].product is None
    assert sorted(boxes.asked(), key=str) == [
        ("model", ["top item", "loafers"], True),
        (1, ["top item"], False),  # the uploaded look shows the top too
    ]
    # the top not boxed: its flat photo; the loafers added
    assert [r.image for r in c.products[0].refs] == [PHOTOS[0], TOP]
    assert _heroes(c)[1][2:4] == ("loafers", SHOES)


def test_with_a_lone_look_photo_the_reference_model_is_ignored(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"midi skirt": SKIRT})
    rows = [look(("bottom", "midi skirt", 1)), look(("bag", "tote", 2))]
    c, _ = _run(fake_gen, rows, model=MODEL)
    assert (c.look_photo, len(c.photos)) == (0, 1)
    assert boxes.asked() == [(0, ["midi skirt"], True)]
    assert _heroes(c) == [("A", "bottom", "midi skirt", SKIRT, "model_crop")]


def test_a_lone_look_photo_is_cut_into_products_whatever_complete_look(
    fake_gen, monkeypatch
):
    Boxes(monkeypatch, {"midi skirt": SKIRT, "loafers": SHOES})
    rows = [
        look(("bottom", "midi skirt", 1), ("footwear", "loafers", 2)),
        look(s="back"),
    ]
    c, crops = _run(fake_gen, rows, complete_look=False)
    assert [(p.id, p.category) for p in c.products] == [
        ("A", "bottom"),
        ("B", "footwear"),
    ]
    assert [r.image for r in c.products[0].refs] == [SKIRT, LOW]
    assert (c.mode, c.framing) == ("outfit", "full_body")
    assert set(crops) == {"top", "bottom", "item-0", "item-1"}


def test_no_product_to_try_on_is_an_input_error(fake_gen, monkeypatch):
    boxes = Boxes(monkeypatch)  # finds nothing
    with pytest.raises(InputError, match="no product to try on"):
        _run(fake_gen, [look(("bottom", "midi skirt", 1))])
    assert len(boxes.calls) == 1
    boxes.calls.clear()
    with pytest.raises(InputError, match="no product to try on"):
        _run(fake_gen, [look(s="back")])  # nothing to box
    assert boxes.calls == []


def test_the_look_fills_up_to_eight_products(fake_gen, monkeypatch):
    bags = [("bag", f"bag {k}", 10 + k) for k in range(4)]
    Boxes(monkeypatch, {f"bag {k}": _crop(10 + k) for k in range(4)})
    rows = [row("top", g, w=True, items=[own("top", g)]) for g in range(1, 7)]
    c, _ = _run(fake_gen, [*rows, look(*bags)])
    assert len(c.products) == reference.MAX_PRODUCTS
    assert [p.description for p in c.products[6:]] == ["bag 0", "bag 1"]


def _polo(fake_gen, monkeypatch, *uploads):
    """The look (photo ``len(uploads)``) adds a scarf and a polo boxed
    like the top half."""
    crops = {"neck scarf": SCARF, "polo shirt": POLO_IMG}
    Boxes(monkeypatch, crops, half=["polo shirt"])
    rows = [
        row(c, g + 1, w=True, items=[own(c, g + 1)])
        for g, c in enumerate(uploads)
    ]
    new = [("jewelry_neck", "neck scarf", 8), ("top", "polo shirt", 9)]
    c, _ = _run(fake_gen, [*rows, look(*new)])
    return c


def test_an_added_top_boxed_like_the_half_gets_it_as_its_photo(
    fake_gen, monkeypatch
):
    """Sent once: no product gets the half as a crop, an added scarf too."""
    skirt, scarf, polo = _polo(fake_gen, monkeypatch, "bottom").products
    assert [(r.image, r.source) for r in polo.refs] == [(TOP, "model_crop")]
    assert [r.image for r in scarf.refs] == [SCARF]
    assert [r.image for r in skirt.refs[1:]] == [LOW]
    products = [skirt, scarf, polo]
    shown = [p.refs[0].image for p in products]
    assert outfit.extra_crops(products, shown) == [(LOW, ["A"])]


@pytest.mark.parametrize("category", reference.BAND_CATEGORIES["top"])
def test_an_uploaded_product_in_the_top_half_no_longer_keeps_it(
    fake_gen, monkeypatch, category
):
    """The polo still takes the half; the half is nobody's crop."""
    c = _polo(fake_gen, monkeypatch, "bottom", category)
    _, uploaded, scarf, polo = c.products
    assert [r.image for r in polo.refs] == [TOP]
    assert [r.image for r in scarf.refs] == [SCARF]
    assert TOP not in [r.image for r in uploaded.refs]
    shown = [p.refs[0].image for p in c.products]
    assert TOP not in dict(outfit.extra_crops(c.products, shown))


def test_an_uploaded_products_hero_crop_takes_the_top_half(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"top item": CROP}, half=["top item"])
    rows = [row("top", 1, items=[own("top", 1)]), look(own("top", 1))]
    c, crops = _run(fake_gen, rows)
    assert boxes.asked() == [(1, ["top item"], True)]
    (top,) = c.products
    assert [(r.image, r.source) for r in top.refs] == [
        (TOP, "model_crop"),  # the half, sent once
        (PHOTOS[0], "upload"),
    ]
    assert crops == {"top": TOP, "bottom": LOW}


def test_the_top_half_goes_to_the_largest_upper_hero_crop(
    fake_gen, monkeypatch
):
    """The polo is largest but its crop was not found: the vest takes the
    half; then the half is sent to nobody else."""
    half = ["polo shirt", "vest"]
    Boxes(monkeypatch, {"vest": VEST_IMG, "top item": CROP}, half=half)
    new = [("top", "polo shirt", 8), ("top", "vest", 9)]
    rows = [row("bottom", 1, w=True, items=[own("bottom", 1)]), look(*new)]
    c, _ = _run(fake_gen, rows)
    assert [(p[2], p[3]) for p in _heroes(c)] == [
        ("bottom item", PHOTOS[0]),
        ("vest", TOP),  # the polo has no crop: dropped
    ]


def _jeans(fake_gen, monkeypatch, *uploads):
    """The look (photo ``len(uploads)``) adds loafers and jeans boxed like
    the bottom half."""
    crops = {"loafers": SHOES, "jeans": JEANS_IMG}
    Boxes(monkeypatch, crops, low=["jeans"])
    rows = [
        row(c, g + 1, w=True, items=[own(c, g + 1)])
        for g, c in enumerate(uploads)
    ]
    new = [("footwear", "loafers", 8), ("bottom", "jeans", 9)]
    return _run(fake_gen, [*rows, look(*new)])


def test_an_added_bottom_boxed_like_the_half_gets_it_as_its_photo(
    fake_gen, monkeypatch
):
    """Sent once: no product gets the half as a crop, added shoes too."""
    c, crops = _jeans(fake_gen, monkeypatch, "top")
    top, loafers, jeans = c.products
    assert [(r.image, r.source) for r in jeans.refs] == [(LOW, "model_crop")]
    assert [r.image for r in loafers.refs] == [SHOES]
    assert [r.image for r in top.refs[1:]] == [TOP]
    shown = [p.refs[0].image for p in c.products]
    assert outfit.extra_crops(c.products, shown) == [(TOP, ["A"])]
    assert crops == {"top": TOP, "bottom": LOW, "item-0": SHOES}


@pytest.mark.parametrize("category", reference.BAND_CATEGORIES["bottom"])
def test_an_uploaded_product_in_the_bottom_half_no_longer_keeps_it(
    fake_gen, monkeypatch, category
):
    """The jeans still take the half; the half is nobody's crop."""
    c, _ = _jeans(fake_gen, monkeypatch, "top", category)
    _, uploaded, loafers, jeans = c.products
    assert [r.image for r in jeans.refs] == [LOW]
    assert [r.image for r in loafers.refs] == [SHOES]
    assert LOW not in [r.image for r in uploaded.refs]
    shown = [p.refs[0].image for p in c.products]
    assert LOW not in dict(outfit.extra_crops(c.products, shown))


def test_an_uploaded_products_hero_crop_takes_the_bottom_half(
    fake_gen, monkeypatch
):
    boxes = Boxes(monkeypatch, {"bottom item": CROP}, low=["bottom item"])
    rows = [row("bottom", 1, items=[own("bottom", 1)]), look(own("bottom", 1))]
    c, crops = _run(fake_gen, rows)
    assert boxes.asked() == [(1, ["bottom item"], True)]
    (bottom,) = c.products
    assert [(r.image, r.source) for r in bottom.refs] == [
        (LOW, "model_crop"),  # the half, sent once
        (PHOTOS[0], "upload"),
    ]
    assert crops == {"top": TOP, "bottom": LOW}


def test_the_bottom_half_goes_to_the_largest_bottom_hero_crop(
    fake_gen, monkeypatch
):
    """The jeans are largest but their crop was not found: the skirt takes
    the half; then the half is sent to nobody else."""
    low = ["jeans", "maxi skirt"]
    Boxes(monkeypatch, {"maxi skirt": SKIRT, "top item": CROP}, low=low)
    new = [("bottom", "jeans", 8), ("bottom", "maxi skirt", 9)]
    rows = [row("top", 1, w=True, items=[own("top", 1)]), look(*new)]
    c, _ = _run(fake_gen, rows)
    assert [(p[2], p[3]) for p in _heroes(c)] == [
        ("top item", PHOTOS[0]),
        ("maxi skirt", LOW),  # the jeans have no crop: dropped
    ]
    assert [r.image for r in c.products[1].refs] == [LOW]


def test_a_covered_uploaded_product_is_never_cropped(fake_gen, monkeypatch):
    """Under the look's jacket: it falls to its flat photo; a front photo
    where it shows uncovered is a source."""
    boxes = Boxes(monkeypatch, {"top item": CROP})
    covered = look(("top", "top item", 1, True), ("top", "jacket", 2))
    c, _ = _run(fake_gen, [row("top", 1, items=[own("top", 1)]), covered])
    assert boxes.asked() == [(1, ["jacket"], True)]
    assert _heroes(c)[0][3] is PHOTOS[0]  # flat
    boxes.calls.clear()
    shown = row("bottom", 3, w=True, items=[own("bottom", 3), own("top", 1)])
    rows = [row("top", 1, items=[own("top", 1)]), shown, covered]
    c, _ = _run(fake_gen, rows)
    assert sorted(boxes.asked()) == [
        (1, ["top item"], False),
        (2, ["jacket"], True),
    ]
    assert _heroes(c)[0][3] == CROP


def test_a_covered_item_the_look_adds_keeps_its_crop(fake_gen, monkeypatch):
    boxes = Boxes(monkeypatch, {"turtleneck": CROP})
    rows = [
        row("top", 1, w=True, items=[own("top", 1)]),
        look(own("top", 1), ("top", "turtleneck", 2, True)),
    ]
    c, _ = _run(fake_gen, rows)
    assert boxes.asked() == [(1, ["turtleneck"], True)]
    assert _heroes(c)[1][2:] == ("turtleneck", CROP, "model_crop")


def test_a_small_crop_gives_way_to_a_flat_front_upload(fake_gen, monkeypatch):
    """Only to a flat front upload: a side packshot does not beat it, and a
    product the look adds keeps its small crop."""
    crops = {"footwear item": TINY, "bag item": TINY, "loafers": TINY}
    Boxes(monkeypatch, crops)
    rows = [
        row("footwear", 1, items=[own("footwear", 1)]),  # flat front
        row("bag", 2, "other", items=[own("bag", 2)]),  # a side view only
        look(own("footwear", 1), own("bag", 2), ("footwear", "loafers", 3)),
    ]
    c, _ = _run(fake_gen, rows)
    assert [(p[3], p[4]) for p in _heroes(c)] == [
        (PHOTOS[0], "upload"),
        (TINY, "model_crop"),
        (TINY, "model_crop"),
    ]
    assert hero.MIN_HERO_SIDE == 400


@pytest.mark.parametrize("short", [149, 150])
def test_a_thin_crop_gives_way_to_a_flat_front_upload(
    fake_gen, monkeypatch, short
):
    """Long enough, but under ``MIN_HERO_SHORT_SIDE`` px across (a bag
    hanging edge-on); a product the look adds keeps its thin crop."""
    thin = _crop(10, short, 600)
    Boxes(monkeypatch, {"bag item": thin, "belt bag": thin})
    rows = [
        row("bag", 1, items=[own("bag", 1)]),  # flat front
        look(own("bag", 1), ("bag", "belt bag", 2)),
    ]
    c, _ = _run(fake_gen, rows)
    uploaded = (thin, "model_crop") if short == 150 else (PHOTOS[0], "upload")
    assert [(p[3], p[4]) for p in _heroes(c)] == [
        uploaded,
        (thin, "model_crop"),
    ]
    assert hero.MIN_HERO_SHORT_SIDE == 150


def test_a_worn_or_flat_photo_that_does_not_list_its_product_is_no_hero(
    fake_gen, monkeypatch
):
    """A zoom of the coat over the sweater, filed as the coat's worn front
    photo: the coat is not whole in it."""
    Boxes(monkeypatch)
    rows = [
        row("top", 1, w=True, items=[("top", "sweater", 9)]),  # lists not A
        row("top", 1, "other", items=[own("top", 1)]),
        row("bag", 2, items=[]),  # a flat packshot that lists nothing
    ]
    c, _ = _run(fake_gen, rows)
    coat, bag = c.products
    # no worn hero: the fallback, its best front upload (the worn photo)
    assert coat.refs[0].image is PHOTOS[0]
    assert bag.refs[0].image is PHOTOS[2]
    tiers = [hero.pick(p, None, c, PHOTOS[:3])[0] for p in (coat, bag)]
    assert tiers == ["fallback", "fallback"]


def test_upscale_crops_upscales_the_added_shoes_only(fake_gen, monkeypatch):
    upscaled = []

    def upscale(crop):
        upscaled.append(crop)
        return UPSCALED

    monkeypatch.setattr(reference, "upscale_shoes", upscale)
    crops = {"loafers": SHOES, "footwear item": OWN_SHOES}
    Boxes(monkeypatch, crops)
    rows = [
        row("footwear", 1, items=[own("footwear", 1)]),  # uploaded shoes
        look(own("footwear", 1), ("footwear", "loafers", 2)),
    ]
    c, named = _run(fake_gen, rows, upscale_crops=True)
    uploaded, added = c.products
    assert uploaded.refs[0].image == OWN_SHOES  # its crop, as it is
    assert upscaled == [SHOES]
    assert added.refs[0].image == UPSCALED
    assert named["item-1"] == UPSCALED
    assert named["item-0"] == OWN_SHOES
