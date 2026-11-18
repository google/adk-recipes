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

"""Tests for the outfit route helpers."""

import re

from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.workflows.tryon import outfit


def _flat(img, rank=1, view="front"):
    return ProductRef(img, view, rank)


def _worn(img, rank=1, view="front"):
    return ProductRef(img, view, rank, presentation="worn")


def _crop(img, rank=1, view="front"):
    return ProductRef(img, view, rank, presentation="worn", source="model_crop")


def test_best_upload_priority(images, crops):
    jeans = Product(
        "A",
        "bottom",
        "jeans",
        [_flat(images[0], 1), _crop(crops[2], 1), _worn(images[1], 2)],
    )
    shirt = Product(
        "B", "top", "shirt", [_flat(images[2], 1), _crop(crops[0], 2)]
    )
    jacket = Product(
        "C", "top", "jacket", [_flat(images[3], 2), _flat(images[4], 1)]
    )
    shoes = Product("D", "footwear", "shoes", [_crop(crops[1], 1)])
    assert [outfit.best_upload(p).image for p in (jeans, shirt, jacket)] == [
        images[1],  # worn upload beats flat and crop
        images[2],  # a flat upload beats a crop
        images[4],  # tie on priority -> better rank
    ]
    assert outfit.best_upload(shoes).image == crops[1]  # no upload: the crop


def test_the_garment_image_is_the_hero_the_first_image(images, crops):
    """``hero`` puts each product's hero first, a crop too."""
    jeans = Product("A", "bottom", "jeans", [_crop(crops[2]), _worn(images[1])])
    shirt = Product("B", "top", "shirt", [_flat(images[2]), _worn(images[3])])
    chosen = outfit.choose_garment_images(
        Classification("outfit", [jeans, shirt], "full_body")
    )
    assert [(p.id, r) for p, r in chosen] == [
        ("A", jeans.refs[0]),
        ("B", shirt.refs[0]),
    ]


def test_extra_crops_once_each_with_the_products_they_show(images, crops):
    blazer = Product("A", "top", "blazer", [_flat(images[0]), _crop(crops[0])])
    coat = Product("B", "full_body_outer", "coat", [_crop(crops[0])])
    jeans = Product("C", "bottom", "jeans", [_flat(images[1]), _crop(crops[2])])
    assert outfit.extra_crops([blazer, coat, jeans], [images[0]]) == [
        (crops[0], ["A", "B"]),
        (crops[2], ["C"]),
    ]
    assert outfit.extra_crops([blazer, coat, jeans], [crops[0]]) == [
        (crops[2], ["C"])
    ]  # a crop already shown as a main image is not repeated


def test_the_styling_plan_sees_the_extra_crops(fake_gen, images, crops):
    fake_gen.json({"items": []})
    jeans = Product("A", "bottom", "jeans", [_flat(images[1])])
    outfit.analyze_outfit([(jeans, jeans.refs[0])], [(crops[2], ["A"])])
    parts = fake_gen.calls[0]["raw_parts"]
    assert parts[-3:-1] == ["CROPPED FROM REFERENCE, PRODUCT A:", crops[2]]
    schema = fake_gen.calls[0]["schema"]["properties"]["items"]["items"]
    assert schema["required"][0] == "product_id"  # a fixed JSON shape


def test_the_styling_plan_keeps_the_closure_of_each_product_image(
    fake_gen, images
):
    fake_gen.json({"items": []})
    shirt = Product("A", "top", "shirt", [_flat(images[0])])
    outfit.analyze_outfit([(shirt, shirt.refs[0])])
    system = fake_gen.calls[0]["system"]
    assert (
        "   - Closure: tailored jackets and coats (blazers, suit jackets, "
        "tailored or belted coats) are worn closed exactly as their product "
        "image shows them (a tied belt stays tied). Casual outer layers "
        "(field, denim, chore and utility jackets, overshirts, cardigans) "
        "worn over another supplied top are worn fully open, so that top's "
        "front shows. Every other garment is worn closed or open exactly as "
        "its product image (FLAT or WORN) shows it. A CROPPED FROM REFERENCE "
        "image or the BRAND STYLING RULES decide first.\n"
    ) in system
    assert "wear the outer layer OPEN" not in system
    assert "Layering:" not in system
    assert "canonical state" not in system
    assert (
        "An inner layer under a closed outer layer shows only what the outer "
        "garment leaves uncovered in its own product image: its armholes, its "
        "neckline exactly as photographed, below its hem. Never write a "
        "visibility that needs the outer garment opened wider than its "
        "product image shows. Example: a blazer under a closed sleeveless wrap "
        'coat: "only its sleeves visible through the armholes".'
    ) in system


def _plan_system(fake_gen, images) -> str:
    fake_gen.json({"items": []})
    shirt = Product("A", "top", "shirt", [_flat(images[0])])
    outfit.analyze_outfit([(shirt, shirt.refs[0])])
    return fake_gen.calls[0]["system"]


def test_the_styling_plan_gives_each_photo_kind_its_role(fake_gen, images):
    system = _plan_system(fake_gen, images)
    assert (
        "- CROPPED FROM REFERENCE: cut from the look photo, the whole outfit "
        "styled on a model. It is the styling authority for the products it "
        "shows: copy their tuck, closure, layering, length, hem, break and "
        "visibility from it, over the outfit rules."
    ) in system
    assert (
        "- WORN: the product's own photo on a person, shot with other "
        "clothes. For tuck and layering it counts as a FLAT image: this "
        "outfit decides them. Its closure is the product's, like a FLAT "
        "image's (the closure rule below). Otherwise it is used for FIT "
        "ONLY: length, break, turn-up or cuff, width at the bottom, whether "
        "trousers go over or into boots, and how it hangs."
    ) in system
    assert "- FLAT: a product photo with no person." in system
    assert (
        "PRIORITY, highest first: 1. the BRAND STYLING RULES, when given: "
        "they apply to every product, whatever its image; where a rule "
        "conflicts with an image, the rule wins. 2. A CROPPED FROM REFERENCE "
        "image. 3. The outfit rules below: the closure rule and the tuck "
        "by the outfit's formality and layers. 4. The fit a WORN "
        "image shows (the hem). 5. The defaults below. Tuck is decided by "
        "the outfit, never taken from a WORN image."
    ) in system


def test_the_styling_plan_never_copies_tuck_from_a_worn_photo(fake_gen, images):
    system = _plan_system(fake_gen, images)
    for sentence in re.split(r"(?<=\.)\s", system):
        assert not re.search(r"\bcopy\b.*\bWORN\b", sentence, re.I), sentence
    assert "Copy the tuck state a CROPPED FROM REFERENCE image shows." in system
    assert (
        "Copy the closure state a CROPPED FROM REFERENCE image shows."
    ) in system
    assert "exactly as shown" not in system


def test_the_styling_plan_states_the_boots_rule(fake_gen, images):
    system = _plan_system(fake_gen, images)
    assert (
        "Tall boots: when the outfit has tall boots and full-length trousers "
        "whose image does not show them tucked in, the hem falls over the "
        "boot shafts and only the feet show."
    ) in system
    assert '"only the feet visible under the trouser hem"' in system
    assert '"hem": "..."' in system


def test_the_styling_plan_gets_the_brand_rules(fake_gen, images):
    fake_gen.json({"items": []})
    shirt = Product("A", "top", "shirt", [_flat(images[0])])
    outfit.analyze_outfit([(shirt, shirt.refs[0])], (), " Coats open. ")
    parts = fake_gen.calls[0]["raw_parts"]
    assert parts[-2:] == [
        "BRAND STYLING RULES: Coats open.",
        "Return the styling plan JSON for this outfit.",
    ]
    outfit.analyze_outfit([(shirt, shirt.refs[0])], (), "  ")
    assert "BRAND STYLING RULES" not in str(fake_gen.calls[1]["raw_parts"])


def test_best_upload_prefers_front(images):
    coat = Product(
        "A",
        "full_body_outer",
        "coat",
        [_worn(images[0], 9, "back"), _flat(images[1])],
    )
    bag = Product("B", "bag", "bag", [_flat(images[2], 3, "other")])
    assert [outfit.best_upload(p).image for p in (coat, bag)] == [
        images[1],
        images[2],
    ]


def test_analyze_outfit_returns_an_item_per_product(fake_gen, images, crops):
    chosen = [
        (
            Product("A", "top", "oxford shirt", [_worn(images[0])]),
            _worn(images[0]),
        ),
        (Product("B", "bottom", "chinos", [_crop(crops[2])]), _crop(crops[2])),
        (Product("C", "top", "blazer", [_flat(images[1])]), _flat(images[1])),
    ]
    fake_gen.json(
        {
            "items": [
                {
                    "product_id": "A",
                    "tuck": "tucked",
                    "closure": "fully closed",
                    "layer_order": 2,
                    "visibility": "collar and placket visible",
                },
                {"product_id": "B", "hem": "full length, a single turn-up"},
                {
                    "product_id": "C",
                    "tuck": "sideways",
                    "closure": "zipped",
                    "layer_order": "1",
                    "visibility": "fully visible",
                },
                {"product_id": "Z", "tuck": "tucked"},
            ],
            "notes": "Ignored: the plan has no free-text notes.",
        }
    )
    plan = outfit.analyze_outfit(chosen)
    assert fake_gen.calls[-1]["params"].model == get_models().judge
    assert fake_gen.calls[-1]["params"].thinking == "MEDIUM"
    assert [i["product_id"] for i in plan.items] == ["A", "B", "C"]
    assert plan.items[0] == {
        "product_id": "A",
        "tuck": "tucked",
        "closure": "fully closed",
        "layer_order": 2,
        "hem": "",
        "visibility": "collar and placket visible",
    }
    assert plan.items[1] == {
        "product_id": "B",
        "tuck": "",
        "closure": "",
        "layer_order": None,
        "hem": "full length, a single turn-up",
        "visibility": "",
    }
    assert (plan.items[2]["tuck"], plan.items[2]["closure"]) == ("", "")
    assert plan.items[2]["layer_order"] is None
    assert not hasattr(plan, "notes")
    labels = [p for p in fake_gen.calls[0]["raw_parts"] if isinstance(p, str)]
    assert labels[0].startswith("PRODUCT A (WORN")
    assert labels[1].startswith("PRODUCT B (CROPPED FROM REFERENCE")
    assert labels[2].startswith("PRODUCT C (FLAT")


def test_analyze_outfit_failure_returns_empty_plan(fake_gen, images):
    fake_gen.json(GenerationError("no json"))
    product = Product("A", "top", "tee", [_flat(images[0])])
    plan = outfit.analyze_outfit([(product, product.refs[0])])
    assert plan.items == []


def test_a_closed_jacket_is_fastened_as_in_its_photo():
    system = outfit._PLAN_SYSTEM
    assert (
        "A jacket or blazer worn closed is fastened as in its product image "
        "(the same buttons fastened)."
    ) in system
    assert "never fasten the bottom button" not in system
    assert "suit rule" not in system
