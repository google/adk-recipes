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

"""Golden-file tests for the composite try-on prompt (both routes).

Regenerate the golden files with ``UPDATE_GOLDEN=1 pytest tests/workflows/tryon``.
"""

import io
import os
import re
from pathlib import Path

import pytest
from PIL import Image

from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import ProductRef
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import (
    cards,
    closeups,
    outfit_prompt,
    styling,
)
from genmedia4commerce.workflows.tryon.prompt import (
    BACK_SYSTEM,
    FRAMING,
    SYSTEM,
    PromptProduct,
    PromptSpec,
    build_prompt,
)
from genmedia4commerce.workflows.tryon.styling import (
    FIT,
    NO_FOOTWEAR,
    SLEEVES,
)
from genmedia4commerce.workflows.tryon.types import CompositionPlan

GOLDEN = Path(__file__).parent / "golden"
SHOWN = "exactly as in its product photo"  # a closed garment, no brand rules
FITTED = "follows the wearer's real body in"
# Re-grading keeps the product's cut.
CUT = (
    "the cut stays as in its product photo: a wide or relaxed leg or sleeve "
    "stays as loose around this body, a slim one stays slim"
)
# The end of an outfit garment's entry.
FITS = (
    f" Its size {FITTED} image_0.png, never the build of the person in its "
    f"photo; {CUT}."
)
SECTIONS = [
    "## INPUTS",
    "## PRODUCTS",
    "## FRAMING",
    "## RULES",
    "## SCENE",
    "Generate the image.",
]
OUTFIT_SECTIONS = [s for s in SECTIONS if s != "## PRODUCTS"]
NAMES = [
    "wearer_body",
    "wearer_face",
    "glasses_front",
    "glasses_three_quarter",
    "shoe_heel_worn",
    "shoe_heel_flat",
    "front_result",
    "jacket_worn",
    "jeans_crop",
    "sneakers_flat",
    "closeup_pocket",
    "closeup_tongue",
    "shirt_flat",
    "trousers_worn",
    "jacket_flat",
    "boots_flat",
    "closeup_pull_tab",
    "detail_photo",
    "look_band",
]
# An outfit's images are drawn again (``cards``): real-looking sizes, so a
# close-up is smaller than its product.
SIZES = {
    "wearer_body": (600, 1200),
    "wearer_face": (300, 360),
    "jacket_worn": (900, 1200),
    "jeans_crop": (600, 1400),
    "sneakers_flat": (1200, 600),
    "closeup_pocket": (200, 200),
    "closeup_tongue": (240, 160),
    "shirt_flat": (900, 1100),
    "trousers_worn": (600, 1500),
    "jacket_flat": (900, 1200),
    "boots_flat": (1100, 1200),
    "closeup_pull_tab": (180, 220),
    "detail_photo": (900, 1200),
    "look_band": (600, 700),
}
IMG = {
    name: png(*SIZES.get(name, (8, 8)), color=(i * 12, 7, 9))
    for i, name in enumerate(NAMES)
}
SCENE = "a plain light grey studio environment"
GUIDELINES = "Jackets closed. No visible logos on the back."
CLOSEUP_END = (
    "Reproduce this detail exactly where it is visible on the product; never "
    "uncover it."
)
IDENTITY = "1. Identity."
# The last sentence of every system text.
STATE_FRAMING = (
    "In your generation description, you MUST state the framing: where the "
    "top and the bottom edge of the photo fall, exactly as the FRAMING "
    "section says."
)
STATE_POSE = (
    "In your generation description, you MUST state the wearer's pose from "
    "the WEARER BODY image (standing or seated, and what they sit on) and "
    "keep it."
)
STATE_CHEST = (
    "In your generation description, you MUST name what covers the wearer's "
    "chest: a product, or the plain top."
)


def single_head_front() -> PromptSpec:
    return PromptSpec(
        framing="head",
        view="front",
        wearer_body=IMG["wearer_body"],
        wearer_face=IMG["wearer_face"],
        products=[
            PromptProduct(
                "A",
                "eyewear",
                [
                    ProductRef(IMG["glasses_front"], "front", 9),
                    ProductRef(IMG["glasses_three_quarter"], "other", 7),
                ],
                "rectangular glasses",
            )
        ],
        scenario=SCENE,
    )


def single_footwear_back() -> PromptSpec:
    return PromptSpec(
        framing="footwear",
        view="back",
        wearer_body=IMG["wearer_body"],
        wearer_face=None,
        products=[
            PromptProduct(
                "A",
                "footwear",
                [
                    ProductRef(
                        IMG["shoe_heel_worn"], "back", 8, presentation="worn"
                    ),
                    ProductRef(IMG["shoe_heel_flat"], "back", 6),
                ],
                "low-top sneakers",
            )
        ],
        scenario=SCENE,
        front_result=IMG["front_result"],
        style_guidelines=GUIDELINES,
    )


def _outfit_products(refs: list[ProductRef]) -> list[PromptProduct]:
    return [
        PromptProduct("A", "top", [refs[0]], "field jacket"),
        PromptProduct("B", "bottom", [refs[1]], "straight jeans"),
        PromptProduct("C", "footwear", [refs[2]], "low-top sneakers"),
    ]


def _item(pid, tuck, closure, layer, hem, visibility) -> dict:
    return {
        "product_id": pid,
        "tuck": tuck,
        "closure": closure,
        "layer_order": layer,
        "hem": hem,
        "visibility": visibility,
    }


NO_CLOSURE = "n/a — no front closure"
PLAN = CompositionPlan(
    items=[
        _item("A", "n/a", "fully closed", 1, "n/a", "fully visible"),
        _item(
            "B",
            "n/a",
            NO_CLOSURE,
            2,
            "full length, a full break over the shoes",
            "lower legs visible below the jacket",
        ),
        _item("C", "", "", None, "", "toe caps visible under the hem"),
    ],
)


def outfit_flat() -> PromptSpec:
    refs = [
        ProductRef(IMG["jacket_worn"], "front", 8),
        ProductRef(IMG["jeans_crop"], "front", 8),
        ProductRef(IMG["sneakers_flat"], "front", 8),
    ]
    return PromptSpec(
        framing="full_body",
        view="front",
        wearer_body=IMG["wearer_body"],
        wearer_face=IMG["wearer_face"],
        products=_outfit_products(refs),
        scenario=SCENE,
        composition_plan=PLAN,
    )


def outfit_worn_crop_flat() -> PromptSpec:
    refs = [
        ProductRef(IMG["jacket_worn"], "front", 8, presentation="worn"),
        ProductRef(
            IMG["jeans_crop"],
            "front",
            6,
            presentation="worn",
            source="model_crop",
        ),
        ProductRef(IMG["sneakers_flat"], "other", 9),
    ]
    return PromptSpec(
        framing="full_body",
        view="front",
        wearer_body=IMG["wearer_body"],
        wearer_face=IMG["wearer_face"],
        products=_outfit_products(refs),
        scenario=SCENE,
        composition_plan=PLAN,
        style_guidelines=GUIDELINES,
    )


POCKET = Detail("hardware", "", "front", "pocket snap", IMG["closeup_pocket"])
TONGUE = Detail("text", "RUN", "front", "tongue label", IMG["closeup_tongue"])
PULL_TAB = Detail("hardware", "", "front", "pull tab", IMG["closeup_pull_tab"])


def outfit_closeups() -> PromptSpec:
    spec = outfit_flat()
    spec.closeups = [("A", POCKET), ("C", TONGUE)]
    return spec


# A close-up found in a zoom photo (``closeups.collect``), named by the
# search: an upload as large as its product's.
BUTTON_PHOTO = Detail(
    closeups.PHOTO,
    "KORVEN'S",
    "other",
    "button on the jeans",
    IMG["detail_photo"],
    name="shank button",
)


def outfit_detail_photo() -> PromptSpec:
    """The jeans' own close-up photo on their card, the sneakers' found
    detail on theirs."""
    spec = outfit_flat()
    spec.closeups = [("B", BUTTON_PHOTO), ("C", TONGUE)]
    return spec


def single_bottom_detail_photo() -> PromptSpec:
    spec = single_head_front()
    spec.framing = "lower_body"
    spec.products = [
        PromptProduct(
            "A",
            "bottom",
            [ProductRef(IMG["trousers_worn"], "front", presentation="worn")],
            "straight jeans",
        )
    ]
    spec.closeups = [("A", BUTTON_PHOTO)]
    return spec


LOOK_PLAN = CompositionPlan(
    items=[
        _item(
            "A",
            "untucked",
            "fully closed",
            2,
            "n/a",
            "only the collar visible at the neckline",
        ),
        _item(
            "B",
            "n/a",
            NO_CLOSURE,
            3,
            "full length, single turn-up, the hem falls over the boot shafts",
            "lower legs visible below the jacket",
        ),
        _item("C", "n/a", "fully closed", 1, "n/a", "fully visible"),
        _item(
            "D",
            "n/a",
            NO_CLOSURE,
            4,
            "n/a",
            "only the feet and soles visible under the trouser hem",
        ),
    ],
)


def outfit_worn_trousers_tall_boots() -> PromptSpec:
    """A flat shirt and field jacket, trousers on a person, tall boots."""
    return PromptSpec(
        framing="full_body",
        view="front",
        wearer_body=IMG["wearer_body"],
        wearer_face=IMG["wearer_face"],
        products=[
            PromptProduct(
                "A",
                "top",
                [ProductRef(IMG["shirt_flat"], "front", 1)],
                "short sleeve shirt",
            ),
            PromptProduct(
                "B",
                "bottom",
                [
                    ProductRef(
                        IMG["trousers_worn"], "front", 1, presentation="worn"
                    )
                ],
                "carpenter trousers",
            ),
            PromptProduct(
                "C",
                "top",
                [ProductRef(IMG["jacket_flat"], "front", 1)],
                "field jacket",
            ),
            PromptProduct(
                "D",
                "footwear",
                [ProductRef(IMG["boots_flat"], "front", 1)],
                "pull-on boots",
            ),
        ],
        scenario=SCENE,
        composition_plan=LOOK_PLAN,
        closeups=[("D", PULL_TAB)],
    )


def outfit_mannequin() -> PromptSpec:
    """The worn jacket and the jeans cut from the look photo, both on a
    mannequin (``worn``), and a band of the look photo, on a mannequin
    too, as the sneakers' REFERENCE CROP; no brand rules."""
    spec = outfit_worn_crop_flat()
    spec.style_guidelines = None
    for product in spec.products[:2]:
        product.refs[0].mannequin = True
    spec.crops = [(IMG["look_band"], ["C"])]
    spec.mannequin_crops = [IMG["look_band"]]
    return spec


def single_mannequin() -> PromptSpec:
    """A field jacket whose worn front photo is on a mannequin (``worn``),
    its flat photo the secondary."""
    spec = single_head_front()
    spec.framing = "upper_body"
    worn = ProductRef(
        IMG["jacket_worn"], "front", presentation="worn", mannequin=True
    )
    flat = ProductRef(IMG["jacket_flat"], "front", 2)
    spec.products = [PromptProduct("A", "top", [worn, flat], "field jacket")]
    return spec


CASES = {
    "single_head_front": single_head_front,
    "single_footwear_back_with_front_result": single_footwear_back,
    "outfit_flat": outfit_flat,
    "outfit_worn_crop_flat_brand_rules": outfit_worn_crop_flat,
    "outfit_closeups": outfit_closeups,
    "outfit_detail_photo": outfit_detail_photo,
    "single_bottom_detail_photo": single_bottom_detail_photo,
    "outfit_worn_trousers_tall_boots": outfit_worn_trousers_tall_boots,
    "outfit_mannequin": outfit_mannequin,
    "single_mannequin": single_mannequin,
}
OUTFITS = sorted(name for name in CASES if name.startswith("outfit_"))


def _size(image: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(image)).size


def _render(system: str, parts: list) -> str:
    """The prompt as text: a fixture image by name, an image drawn for the
    outfit route by its name and size."""
    names = {v: k for k, v in IMG.items()}
    images = [p for p in parts if isinstance(p, bytes)]
    lines = []
    for p in parts:
        if not isinstance(p, bytes):
            lines.append(p)
        elif p in names:
            lines.append(f"[image: {names[p]}]")
        else:
            w, h = _size(p)
            lines.append(f"[image_{images.index(p)}.png: {w}x{h} px]")
    body = "\n".join(lines)
    return f"=== SYSTEM ===\n{system}\n\n=== USER MESSAGE ===\n{body}\n"


def _text(parts: list) -> str:
    return "\n".join(p for p in parts if isinstance(p, str))


def _products(spec: PromptSpec) -> list[str]:
    """How each product is worn: the PRODUCTS section of a single product,
    the styling lines an outfit's entries carry."""
    return styling.product_lines(spec)


def _spec(products, framing="full_body", plan=None) -> PromptSpec:
    """``products``: (category, name, presentation) per product."""
    spec = single_head_front()
    spec.framing = framing
    spec.composition_plan = plan
    spec.products = [
        PromptProduct(
            chr(65 + i),
            category,
            [ProductRef(IMG["shirt_flat"], "front", presentation=worn)],
            name,
        )
        for i, (category, name, worn) in enumerate(products)
    ]
    return spec


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden(name):
    system, parts = build_prompt(CASES[name]())
    rendered = _render(system, parts)
    path = GOLDEN / f"{name}.txt"
    if os.getenv("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
    assert rendered == path.read_text()


@pytest.mark.parametrize("name", sorted(CASES))
def test_structure(name):
    spec = CASES[name]()
    system, parts = build_prompt(spec)
    text = _text(parts)
    outfit = len(spec.products) > 1
    single = BACK_SYSTEM if spec.front_result is not None else SYSTEM
    assert (system == single) is not outfit
    assert "{" not in system + text and "}" not in system + text
    sections = OUTFIT_SECTIONS if outfit else SECTIONS
    positions = [text.find(section) for section in sections]
    assert -1 not in positions
    assert positions == sorted(positions)
    assert text.rstrip().endswith("Generate the image.")
    brand = text.find("Brand styling rules: ")
    assert (brand >= 0) is bool(spec.style_guidelines)
    if spec.style_guidelines:
        rules, scene = positions[-3], positions[-2]
        assert rules < brand < scene


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_rule_is_left_for_the_model_to_resolve(name):
    system, parts = build_prompt(CASES[name]())
    text = f"{system}\n{_text(parts)}".lower()
    for wording in (
        "unless",
        "overrides",
        "for every worn",
        "only where a worn image",
        "defaults",
        "product details",
        "n/a",
    ):
        assert wording not in text, wording


@pytest.mark.parametrize("name", sorted(CASES))
def test_image_parts_follow_their_legend_line(name):
    """'- IMAGE 1 — ' for a single product, '- image_0.png — ' for an
    outfit."""
    _, parts = build_prompt(CASES[name]())
    images = [i for i, p in enumerate(parts) if isinstance(p, bytes)]
    single = "- IMAGE" in _text(parts)
    legend = re.findall(
        r"^- (?:IMAGE \d+|image_\d+\.png) — ", _text(parts), flags=re.MULTILINE
    )
    assert len(images) == len(legend)
    for n, index in enumerate(images, 1):
        label = parts[index - 1]
        name = f"IMAGE {n}" if single else f"image_{n - 1}.png"
        assert isinstance(label, str)
        assert label.splitlines()[-1].startswith(f"- {name} — ")


@pytest.mark.parametrize(
    ("framing", "face", "feet"),
    [
        ("head", True, False),
        ("upper_body", True, False),
        ("lower_body", False, True),
        ("footwear", False, True),
        ("full_body", True, True),
        ("hands", False, False),
    ],
)
def test_identity_where_the_face_shows_ground_shadow_where_feet_show(
    framing, face, feet
):
    spec = single_head_front()
    spec.framing = framing
    text = _text(build_prompt(spec)[1])
    assert ("Identity. The face matches WEARER FACE" in text) is face
    assert ("Hairstyle, hair colour and length stay" in text) is face
    assert (IDENTITY if face else "1. Body.") in text
    assert ("Keep the head angle, gaze and facial expression" in text) is face
    assert ("a natural soft shadow on the ground" in text) is feet
    assert "contact shadows where fabric meets the body" in text
    assert "not pasted into it" in text


def test_the_covered_parts_rule_holds_for_every_product():
    """With its examples for a single product, without for an outfit."""
    for name, case in CASES.items():
        text = _text(build_prompt(case())[1])
        examples = (
            ""
            if name in OUTFITS
            else " (boot shafts under a trouser hem, a shirt under a jacket)"
        )
        assert (
            "Covered parts stay covered: when one product covers part of "
            f"another{examples}, reproduce only the visible part; never "
            "shorten, lift, open, tuck or move a product to show another."
        ) in text, name


NOT_BARE = (
    "No bare chest or breasts and no underwear on show: where neither a "
    "product nor anything else the wearer wears covers the chest, the "
    "wearer wears a plain top, and where nothing covers the hips, a plain "
    "bottom, each in a neutral colour that suits the outfit."
)


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_bare_chest_or_underwear_after_what_else_is_worn(name):
    """Every route (a single product, its back view, an outfit): right
    after the rule on what else the wearer wears, before covered parts."""
    rules = _rules(_text(build_prompt(CASES[name]())[1]))
    only = next(
        i for i, r in enumerate(rules) if "Only the supplied products." in r
    )
    assert rules[only + 1] == f"{only + 2}. {NOT_BARE}"
    assert rules[only + 2].startswith(f"{only + 3}. Covered parts stay")


AS_PHOTO = "worn as its product photo shows it (closed stays closed)"


@pytest.mark.parametrize(
    ("category", "name", "outer"),
    [
        ("full_body_outer", "trench coat", True),
        ("top", "wool blazer", True),
        ("top", "knitted CARDIGAN", True),
        ("top", "quilted jackets", True),
        ("top", "overcoat", True),
        ("top", "t-shirt", False),
        ("top", "coated shirt", False),
        ("bottom", "suit trousers", False),
        ("eyewear", "acetate glasses", False),
    ],
)
def test_a_flat_jacket_alone_is_worn_as_photographed(category, name, outer):
    (line,) = _products(_spec([(category, name, "flat")], "head"))
    assert (line == f"A — {name}: {AS_PHOTO}; {SLEEVES}.") is outer


@pytest.mark.parametrize(
    ("name", "brand", "sleeves"),
    [
        ("trench coat", False, True),
        ("long puffer vest", False, False),  # sleeveless
        ("wool cape coat", False, False),
        ("trench coat", True, False),  # the brand rules decide
    ],
)
def test_outerwear_with_sleeves_is_worn_with_the_arms_through_them(
    name, brand, sleeves
):
    spec = _spec([("full_body_outer", name, "flat")], "head")
    spec.style_guidelines = "Coats are worn open." if brand else None
    (line,) = _products(spec)
    assert (SLEEVES in line) is sleeves


def test_a_casual_jacket_over_a_supplied_shirt_is_open():
    """A casual outer layer (field jacket) over a supplied top is worn
    open, so the top shows; a tailored one keeps its photo closure."""
    spec = _spec(
        [("top", "short sleeve shirt", "flat"), ("top", "field jacket", "flat")]
    )
    assert _products(spec) == [
        "A — short sleeve shirt: worn untucked, under the field jacket (B); "
        "its front shows between the open fronts.",
        "B — field jacket: worn open (unzipped or unbuttoned), over the short "
        f"sleeve shirt (A); {SLEEVES}.",
        NO_FOOTWEAR,
    ]


def test_a_tailored_blazer_over_a_supplied_shirt_keeps_its_photo_closure():
    spec = _spec(
        [("top", "oxford shirt", "flat"), ("top", "wool blazer", "flat")]
    )
    lines = _products(spec)
    assert (
        lines[1]
        == f"B — wool blazer: {AS_PHOTO}, over the oxford shirt (A); {SLEEVES}."
    )
    assert (
        lines[0]
        == "A — oxford shirt: worn untucked, under the wool blazer (B)."
    )


@pytest.mark.parametrize(
    ("under", "jacket"),
    [
        ("t-shirt", f"{AS_PHOTO}, over the t-shirt (B)"),
        ("knitted cardigan", AS_PHOTO),  # outerwear over outerwear
    ],
)
def test_a_long_coat_over_a_supplied_top_keeps_its_photo_closure(under, jacket):
    spec = _spec(
        [("full_body_outer", "trench coat", "flat"), ("top", under, "flat")]
    )
    assert _products(spec)[0] == f"A — trench coat: {jacket}; {SLEEVES}."


@pytest.mark.parametrize(
    ("category", "name"), [("bottom", "jeans"), ("dress", "slip dress")]
)
def test_a_flat_jacket_with_no_top_under_it_is_worn_as_photographed(
    category, name
):
    spec = _spec([("top", "field jacket", "flat"), (category, name, "flat")])
    assert _products(spec)[:2] == [
        f"A — field jacket: {AS_PHOTO}; {SLEEVES}.",
        f"B — {name}.",
    ]


def test_a_worn_jacket_gets_the_defaults_and_its_fit():
    """Its photo on a person shows its closure and its fit; the outfit
    puts it over the supplied shirt."""
    spec = _spec(
        [("top", "short sleeve shirt", "flat"), ("top", "field jacket", "worn")]
    )
    assert _products(spec)[:2] == [
        "A — short sleeve shirt: worn untucked, under the field jacket (B); "
        "its front shows between the open fronts.",
        "B — field jacket: worn open (unzipped or unbuttoned), over the short "
        f"sleeve shirt (A); {SLEEVES}; {FIT}.",
    ]


def test_a_worn_shirt_alone_is_untucked_with_its_fit():
    spec = _spec([("top", "oxford shirt", "worn")], "upper_body")
    assert _products(spec) == [f"A — oxford shirt: worn untucked; {FIT}."]


def test_a_worn_jacket_follows_the_plan_closure_not_its_photo():
    plan = CompositionPlan(
        items=[
            _item("A", "untucked", NO_CLOSURE, 2, "n/a", ""),
            _item("B", "n/a", "fully open", 1, "n/a", ""),
        ]
    )
    spec = _spec(
        [("top", "t-shirt", "flat"), ("top", "field jacket", "worn")],
        plan=plan,
    )
    line = _products(spec)[1]
    assert line == (
        f"B — field jacket: worn fully open, the outermost layer; {SLEEVES}; "
        f"{FIT}."
    )
    assert "exactly as in its photo" not in line


def test_the_plan_tuck_wins_over_a_worn_photo():
    """The shirt's photo on a person shows it tucked (other clothes): the
    outfit's plan decides the tuck."""
    plan = CompositionPlan(
        items=[_item("A", "untucked", "fully closed", 1, "n/a", "")]
    )
    spec = _spec([("top", "oxford shirt", "worn")], "upper_body", plan)
    assert _products(spec) == [
        f"A — oxford shirt: worn untucked, fully closed, {SHOWN}; {FIT}."
    ]


def test_a_reference_crop_wins_over_a_worn_photo():
    spec = _spec([("top", "field jacket", "worn"), ("top", "t-shirt", "flat")])
    spec.crops = [(IMG["jeans_crop"], ["A"])]
    line = _products(spec)[0]
    assert line == "A — field jacket: worn exactly as in its REFERENCE CROP."
    assert FIT not in line


def test_worn_trousers_carry_the_plan_hem_and_the_boots_show_only_the_feet():
    lines = _products(outfit_worn_trousers_tall_boots())
    assert lines == [
        f"A — short sleeve shirt: worn untucked, fully closed, {SHOWN}, under "
        "the field jacket (C); only the collar visible at the neckline.",
        "B — carpenter trousers: full length, single turn-up, the hem falls "
        "over the boot shafts; lower legs visible below the jacket; its photo "
        "on a person shows its fit.",
        f"C — field jacket: worn fully closed, {SHOWN}, the outermost layer; "
        f"{SLEEVES}; fully visible.",
        "D — pull-on boots: only the feet and soles visible under the trouser "
        "hem.",
    ]


def test_the_plan_wins_over_the_defaults_for_flat_products():
    plan = CompositionPlan(
        items=[
            _item("A", "tucked", "fully closed", 2, "n/a", ""),
            _item("B", "n/a", "fully closed", 1, "n/a", ""),
        ]
    )
    spec = _spec(
        [("top", "oxford shirt", "flat"), ("top", "wool blazer", "flat")],
        plan=plan,
    )
    assert _products(spec)[:2] == [
        f"A — oxford shirt: worn tucked, fully closed, {SHOWN}, under the "
        "wool blazer (B).",
        f"B — wool blazer: worn fully closed, {SHOWN}, the outermost layer; "
        f"{SLEEVES}.",
    ]


def test_a_product_without_a_plan_item_gets_the_defaults():
    plan = CompositionPlan(items=[_item("A", "", "", None, "", "")])
    spec = _spec([("top", "field jacket", "flat")], plan=plan)
    assert _products(spec)[0] == f"A — field jacket: {AS_PHOTO}; {SLEEVES}."


def test_a_reference_crop_shows_how_its_products_are_worn():
    spec = outfit_worn_crop_flat()
    spec.style_guidelines = None
    spec.crops = [(IMG["jeans_crop"], ["A", "C"])]
    lines = _products(spec)
    assert lines[0] == (
        "A — field jacket: worn exactly as in its REFERENCE CROP: fully "
        "closed; fully visible."
    )
    assert lines[2] == (
        "C — low-top sneakers: worn exactly as in its REFERENCE CROP: toe "
        "caps visible under the hem."
    )


def test_with_brand_rules_the_plan_comes_before_the_photo():
    """The plan applied the brand rules: its fields first, the photo for
    everything else."""
    assert _products(outfit_worn_crop_flat())[:2] == [
        f"A — field jacket: worn fully closed; fully visible; {FIT}.",
        "B — straight jeans: full length, a full break over the shoes; lower "
        "legs visible below the jacket; everything else exactly as in its "
        "photo on another person.",
    ]


@pytest.mark.parametrize(
    ("products", "lines"),
    [
        ([("top", "field jacket", "flat")], ["A — field jacket."]),
        (
            [("top", "oxford shirt", "flat"), ("top", "wool blazer", "flat")],
            ["A — oxford shirt.", "B — wool blazer."],
        ),
        ([("top", "oxford shirt", "worn")], [f"A — oxford shirt: {FIT}."]),
    ],
)
def test_with_brand_rules_and_no_plan_no_default_is_written(products, lines):
    spec = _spec(products, "upper_body")
    spec.style_guidelines = "Coats open."
    assert _products(spec) == lines
    text = _text(build_prompt(spec)[1])
    assert "\n\nBrand styling rules: Coats open.\n\n## SCENE" in text


@pytest.mark.parametrize(
    ("framing", "products", "shoes"),
    [
        ("full_body", [("dress", "slip dress", "flat")], True),
        ("lower_body", [("bottom", "jeans", "flat")], True),
        ("upper_body", [("top", "t-shirt", "flat")], False),
        ("full_body", [("footwear", "boots", "flat")], False),
    ],
)
def test_plain_shoes_only_when_feet_show_and_none_is_supplied(
    framing, products, shoes
):
    assert (NO_FOOTWEAR in _products(_spec(products, framing))) is shoes


def _images(parts: list) -> list[bytes]:
    return [p for p in parts if isinstance(p, bytes)]


def _entry(text: str, image: str) -> str:
    """The INPUTS entry of ``image`` ('image_2.png')."""
    prefix = f"- {image} — "
    return next(line for line in text.splitlines() if line.startswith(prefix))


def test_an_outfits_body_keeps_the_photos_canvas():
    """The wearer photo goes whole (what the person sits on or uses stays):
    never cut to its content, unlike the face and the products."""
    photo = Image.new("RGB", (600, 800), "white")  # 3:4
    photo.paste((40, 40, 40), (250, 100, 350, 700))  # a narrow figure
    buf = io.BytesIO()
    photo.save(buf, format="PNG")
    spec = outfit_flat()
    spec.wearer_body = buf.getvalue()
    assert cards.tight(spec.wearer_body).size == (112, 612)  # if cut
    _, parts = build_prompt(spec)
    assert _size(_images(parts)[0]) == (960, 1280)  # the photo's 3:4


def _rules(text: str) -> list[str]:
    rules = text.split("## RULES\n", maxsplit=1)[1]
    return rules.split("\n\n", maxsplit=1)[0].splitlines()


def test_an_outfit_gets_one_image_per_product_then_its_crops():
    spec = outfit_worn_crop_flat()
    spec.crops = [(IMG["jeans_crop"], ["B", "C"])]
    system, parts = build_prompt(spec)
    images = _images(parts)
    assert len(images) == 6  # body, face, A, B, C, the crop
    assert not set(images) & set(IMG.values())  # each one drawn again
    for image in images:  # no close-ups: about 1.2 MP each, no text
        w, h = _size(image)
        assert w % 16 == 0 and h % 16 == 0
        assert abs(w * h - 960 * 1280) < 0.05 * 960 * 1280
    names = ", ".join(f"image_{k}.png" for k in range(6))
    assert system == outfit_prompt.SYSTEM.format(
        n=6,
        first_to_last="image_0.png through image_5.png",
        all=names,
        body="image_0.png",
        skin_from="image_1.png and image_0.png",
        face="image_1.png",
        wearer="image_0.png and image_1.png",
        products="image_2.png, image_3.png and image_4.png",
        others="image_2.png, image_3.png, image_4.png and image_5.png",
    )
    assert (
        f"you MUST explicitly reference all 6 input filenames in ascending "
        f"order ({names})"
    ) in system
    text = _text(parts)
    assert _entry(text, "image_5.png") == (
        "- image_5.png — REFERENCE CROP: PRODUCT B straight jeans and PRODUCT "
        "C low-top sneakers as worn by another person, cropped from a photo "
        "of them; use it only for how it is worn and for its details; never "
        "copy that person's body, face or skin tone."
    )
    assert "## PRODUCTS" not in text


def test_each_product_entry_says_how_it_is_worn_naming_the_images():
    text = _text(build_prompt(outfit_worn_trousers_tall_boots())[1])
    assert _entry(text, "image_2.png") == (
        "- image_2.png — PRODUCT A (short sleeve shirt): worn untucked, fully "
        f"closed, {SHOWN}, under the field jacket (image_4.png); only the "
        f"collar visible at the neckline.{FITS}"
    )
    assert _entry(text, "image_4.png") == (
        "- image_4.png — PRODUCT C (field jacket): worn fully closed, "
        f"{SHOWN}, the outermost layer; {SLEEVES}; fully visible.{FITS}"
    )


def test_a_button_row_stays_on_its_card_whatever_its_label_says():
    """The label shortens 'center front closure (11 buttons)' to 'center
    front closure'; the row is still told by what the search said it is,
    so its long crop stays (a crop that tall is otherwise dropped)."""
    row = Detail(
        "hardware",
        "",
        "front",
        "center front closure (11 buttons)",
        png(100, 3000, (1, 2, 3)),
    )
    spec = outfit_flat()
    spec.closeups = [("A", row), ("A", POCKET)]
    text = _text(build_prompt(spec)[1])
    entry = _entry(text, "image_2.png")
    assert "Details shown in image_2.png: A1 center front closure" in entry
    assert "A2 pocket snap" in entry


def test_a_named_closeup_is_labelled_by_what_it_is():
    zip_pull = Detail(
        "hardware",
        "",
        "right",
        "top of right side seam",
        IMG["closeup_pocket"],
        name="side zip",
    )
    spec = outfit_flat()
    spec.closeups = [("B", zip_pull)]
    entry = _entry(_text(build_prompt(spec)[1]), "image_3.png")
    assert entry.endswith(
        "Detail shown in image_3.png: B1 side zip (top of right side seam)."
        + FITS
    )


def test_closeups_go_on_their_product_card_coded_after_filtering():
    """A crop wider than 60% of its product is dropped; the others are
    coded again, so the prompt never names the dropped one."""
    placket = Detail(
        "hardware", "", "front", "front panel", png(560, 100, (1, 2, 3))
    )
    spec = outfit_flat()
    spec.closeups = [("A", placket), ("A", POCKET), ("C", TONGUE)]
    _, parts = build_prompt(spec)
    text = _text(parts)
    assert _entry(text, "image_2.png") == (
        "- image_2.png — PRODUCT A (field jacket) and its labelled detail "
        f"close-up beside it: worn fully closed, {SHOWN}; {SLEEVES}; fully "
        "visible. "
        f"Detail shown in image_2.png: A1 pocket snap.{FITS}"
    )
    assert "front panel" not in text and "A2" not in text
    assert _entry(text, "image_3.png") == (
        "- image_3.png — PRODUCT B (straight jeans): full length, a full "
        f"break over the shoes; lower legs visible below the jacket.{FITS}"
    )
    assert _entry(text, "image_4.png").endswith(
        "Detail shown in image_4.png: C1 RUN lettering (tongue label)."
    )
    assert _rules(text)[2].endswith(
        "Each detail close-up (A1 in image_2.png; C1 in image_4.png) is an "
        "enlarged crop of its product photo: reproduce that detail exactly, "
        "at its true size, where it is visible on its product; never "
        "uncover it."
    )


def test_outfit_rules_name_the_images():
    rules = _rules(_text(build_prompt(outfit_closeups())[1]))
    assert rules[0].startswith(f"{IDENTITY} The face matches image_1.png ")
    assert "stay as in image_0.png (WEARER BODY)." in rules[0]
    assert (
        "Keep the head angle, gaze and facial expression of image_1.png"
        in (rules[0])
    )
    assert rules[1].startswith(
        "2. Body. Keep image_0.png (WEARER BODY) exactly as it is"
    )
    assert rules[2].startswith(
        "3. Product fidelity. Reproduce each product from image_2.png, "
        "image_3.png and image_4.png and its detail close-ups from "
        "image_2.png and image_4.png exactly: "
    )
    assert rules[3].startswith(
        "4. No invented detail. Surfaces not visible in image_2.png, "
        "image_3.png and image_4.png stay plain"
    )
    assert rules[4].endswith("Any text reads correctly left-to-right.")
    assert "(for example" not in rules[5]
    assert rules[5].endswith(
        "Never copy any person, body or face from image_2.png, image_3.png "
        "and image_4.png."
    )
    assert rules[9] == (
        "10. Output exactly one image of one person. No duplicates, split "
        "frames, collage borders, label codes, text or watermarks."
    )


def test_without_a_face_the_outfit_identity_rule_uses_the_body():
    spec = outfit_flat()
    spec.wearer_face = None
    system, parts = build_prompt(spec)
    assert "body come only from image_0.png;" in system
    rule = _rules(_text(parts))[0]
    assert "The face matches image_0.png exactly" in rule
    assert "facial expression of image_0.png exactly" in rule
    assert "of the wearer in image_0.png (" in system


@pytest.mark.parametrize(
    ("framing", "ratio", "shape"),
    [
        ("full_body", "3:4", "Vertical 3:4 full-length portrait. "),
        ("upper_body", "3:4", "Vertical 3:4 portrait. "),
        ("full_body", "9:16", ""),
        ("upper_body", "1:1", ""),
    ],
)
def test_an_outfit_framing_names_the_portrait_only_when_it_is_3_4(
    framing, ratio, shape
):
    spec = outfit_flat()
    spec.framing, spec.aspect_ratio = framing, ratio
    text = _text(build_prompt(spec)[1])
    assert (
        f"## FRAMING\n{shape}{FRAMING[framing]}\nShow the wearer from the "
        "front.\n\n## RULES"
    ) in text


def test_an_outfit_without_shoes_gets_plain_shoes_under_framing():
    spec = _spec([("top", "t-shirt", "flat"), ("bottom", "jeans", "flat")])
    text = _text(build_prompt(spec)[1])
    assert f"Show the wearer from the front.\n{NO_FOOTWEAR}\n\n## RULES" in text


def test_a_single_product_keeps_its_closeups_as_separate_images():
    spec = single_head_front()
    spec.closeups = [("A", POCKET)]
    _, parts = build_prompt(spec)
    assert parts[-2] == IMG["closeup_pocket"]
    line = parts[-3].splitlines()[-1]
    assert line.startswith(
        "- IMAGE 5 — DETAIL CLOSE-UP of PRODUCT A rectangular glasses "
        "(hardware; pocket snap): a crop of its product photo, shown enlarged"
    )
    assert line.endswith(CLOSEUP_END)


def test_single_route_has_secondary_angle_rule_and_category_note():
    _, parts = build_prompt(single_head_front())
    text = _text(parts)
    assert (
        "PRODUCT A rectangular glasses (flat photo, front view): shows how "
        "the product looks and sets the camera angle."
    ) in text
    assert (
        "PRODUCT A rectangular glasses, secondary reference (flat photo)"
        in text
    )
    assert "Match the camera angle of PRODUCT A's primary reference" in text
    assert "Lenses keep the reference's tint" in text
    assert "Show the wearer from the front." in text


def test_back_pass_attaches_the_front_result_last():
    _, parts = build_prompt(single_footwear_back())
    assert parts[-2] == IMG["front_result"]
    text = _text(parts)
    assert "Show the wearer from the back." in text
    assert "Identity." not in text  # footwear framing: no face


@pytest.mark.parametrize("framing", ["footwear", "upper_body"])
def test_back_pass_sends_only_the_back_photos_and_the_front_result(framing):
    spec = single_footwear_back()
    spec.framing = framing
    spec.wearer_face = IMG["wearer_face"]  # given, never sent
    system, parts = build_prompt(spec)
    assert [p for p in parts if isinstance(p, bytes)] == [
        IMG["shoe_heel_worn"],
        IMG["shoe_heel_flat"],
        IMG["front_result"],
    ]
    text = system + _text(parts)
    assert "WEARER" not in text
    assert ("Identity. The wearer is the person in FRONT RESULT" in text) is (
        framing == "upper_body"
    )


def test_empty_slots_are_omitted():
    spec = single_head_front()
    spec.products[0].description = ""
    spec.wearer_face = None
    _, parts = build_prompt(spec)
    text = _text(parts)
    assert "Brand styling rules" not in text
    assert "- IMAGE 2 — PRODUCT A (flat photo, front view): " in text
    assert "## PRODUCTS\nA.\n" in text
    assert "The face matches WEARER BODY exactly" in text


def test_a_lining_colour_stays_inside():
    assert (
        "A garment's lining or inside colour, seen at its neckline or opening "
        "in the product photo, stays inside: never on its lapels, collar, "
        "front edges or outer surface."
    ) in outfit_prompt.RULE_INVENTED


FIT_RULE = "The garment size and fit are the wearer's:"


def _fidelity(text: str) -> str:
    return next(r for r in _rules(text) if "Product fidelity." in r)


def test_only_garment_entries_follow_the_wearer_body():
    text = _text(build_prompt(outfit_flat())[1])
    for image, garment in [
        ("image_2.png", True),  # field jacket
        ("image_3.png", True),  # jeans
        ("image_4.png", False),  # sneakers
    ]:
        assert _entry(text, image).endswith(FITS) is garment


@pytest.mark.parametrize(
    ("category", "back", "body"),
    [
        ("bottom", False, "WEARER BODY"),
        ("full_body_outer", True, "FRONT RESULT"),
        ("footwear", False, None),
        ("eyewear", True, None),
    ],
)
def test_a_single_garment_follows_the_wearer_body(category, back, body):
    """The primary product line and Rule 3, naming FRONT RESULT in the back
    view, which sends no WEARER image."""
    spec = single_footwear_back() if back else single_head_front()
    spec.products[0].category = category
    system, parts = build_prompt(spec)
    text = _text(parts)
    if body is None:
        assert FITTED not in text and FIT_RULE not in text
        return
    primary = next(line for line in text.splitlines() if "camera angle" in line)
    assert primary.endswith(
        f"{FITTED} {body}, never the build of the person in its photo; {CUT}."
    )
    assert _fidelity(text).endswith(
        "resize any of these design elements. The garment size and fit are "
        "the wearer's: waist, belly, hips, seat, chest, coat opening, sleeve "
        "width and trouser-leg width are re-graded to fit the exact body mass "
        f"and thigh thickness in {body}, never kept at the catalogue model's "
        f"width or thigh gap; {CUT}."
    )
    assert ("WEARER" in system + text) is not back


@pytest.mark.parametrize(
    ("products", "garment"),
    [
        ([("top", "t-shirt", "flat"), ("footwear", "boots", "flat")], True),
        ([("footwear", "boots", "flat"), ("bag", "tote bag", "flat")], False),
    ],
)
def test_outfit_fidelity_regrades_only_when_a_garment_is_supplied(
    products, garment
):
    text = _text(build_prompt(_spec(products))[1])
    assert (FIT_RULE in _fidelity(text)) is garment
    assert (
        "thigh thickness in image_0.png, never kept at the catalogue "
        "model's width" in _fidelity(text)
    ) is garment
    assert (FITTED in text) is garment


@pytest.mark.parametrize(
    ("case", "body", "others"),
    [
        (single_head_front, "the WEARER images", "the PRODUCT images"),
        (single_footwear_back, "FRONT RESULT", "the PRODUCT images"),
        (
            outfit_flat,
            "image_0.png",
            "image_2.png, image_3.png and image_4.png",
        ),
    ],
)
def test_every_system_asks_for_the_wearer_build(case, body, others):
    """A sentence of its own after the system text."""
    system, _ = build_prompt(case())
    assert (
        "nothing else is invented. In your generation description, you MUST "
        "also include a dedicated sentence that names the wearer's actual "
        f"physical build from {body} in concrete visual terms (for example, "
        f"if the wearer in {body} is plus-size, heavy-set or full-figured"
    ) in system
    assert system.endswith(
        f"never copying the narrow hips, slim trouser legs or thigh gap of the "
        f"people shown in {others}; {CUT}. {STATE_FRAMING}"
        + (
            ""
            if case is single_footwear_back
            else f" {STATE_POSE} {STATE_CHEST}"
        )
    )


@pytest.mark.parametrize(
    ("case", "skin_from", "face", "others"),
    [
        (
            single_head_front,
            "the WEARER images",
            "the skin of the WEARER images",
            "the PRODUCT images",
        ),
        (
            single_footwear_back,
            "FRONT RESULT",
            "FRONT RESULT",
            "the PRODUCT images",
        ),
        (
            outfit_flat,
            "image_1.png and image_0.png",
            "image_1.png",
            "image_2.png, image_3.png and image_4.png",
        ),
    ],
)
def test_every_system_states_the_wearer_skin_shade_and_brightness(
    case, skin_from, face, others
):
    """Face included, never darker or lighter toward the people in the
    product images."""
    system, _ = build_prompt(case())
    assert (
        "In your generation description, you MUST explicitly state the exact "
        "skin tone, shade, undertone, and brightness/lightness of the wearer "
        f"in {skin_from} (for example: fair, light tan, medium warm "
        "golden-brown, or deep dark brown) and state that all of the "
        "wearer's visible skin, the face included, keeps the exact skin "
        f"brightness and warm/cool shade of {face} without darkening or lightening "
        f"toward the skin tone of the people shown in {others}. The wearer's "
        "identity and body come only from "
    ) in system
    assert "never the skin tone or limbs" not in system


def test_the_wearer_face_keeps_its_skin_brightness():
    _, parts = build_prompt(single_head_front())
    assert (
        "- IMAGE 2 — WEARER FACE: authoritative for facial identity, "
        "features, skin texture, and the exact skin tone, shade and "
        "brightness."
    ) in _text(parts)


@pytest.mark.parametrize(
    ("case", "face", "body"),
    [
        (single_head_front, "WEARER FACE", "WEARER BODY"),
        (outfit_flat, "image_1.png", "image_0.png (WEARER BODY)"),
    ],
)
def test_rule_1_keeps_the_face_skin_brightness(case, face, body):
    rule = _rules(_text(build_prompt(case())[1]))[0]
    assert rule == (
        f"{IDENTITY} The face matches {face} exactly: features, skin "
        "texture, skin shade and brightness. Hairstyle, hair colour "
        f"and length stay as in {body}. Do not beautify, age, slim or reshape "
        f"the wearer. Keep the head angle, gaze and facial expression of "
        f"{face} exactly: a face in profile stays in profile, a three-quarter "
        "face stays three-quarter, a smile stays a smile; never turn the face "
        "towards the camera or copy the head angle of a catalogue model."
    )


ONLY = "take ONLY the {} from it, never that person's face, skin or body"


def test_a_worn_product_gives_only_itself_a_flat_one_says_nothing():
    """Its own image shows a person: take only the product from it."""
    spec = _spec(
        [("top", "oxford shirt", "worn"), ("bottom", "jeans", "flat")],
        "upper_body",
    )
    text = _text(build_prompt(spec)[1])
    assert _entry(text, "image_2.png") == (
        "- image_2.png — PRODUCT A (oxford shirt), shown on a different "
        f"person: {ONLY.format('oxford shirt')}; worn untucked; {FIT}.{FITS}"
    )
    assert _entry(text, "image_3.png") == (
        f"- image_3.png — PRODUCT B (jeans).{FITS}"
    )


def test_a_worn_product_with_a_reference_crop_is_worn_as_in_it():
    spec = _spec([("top", "field jacket", "worn"), ("top", "t-shirt", "flat")])
    spec.crops = [(IMG["jeans_crop"], ["A"])]
    entry = _entry(_text(build_prompt(spec)[1]), "image_2.png")
    assert entry == (
        "- image_2.png — PRODUCT A (field jacket), shown on a different "
        f"person: {ONLY.format('field jacket')}; worn as in its REFERENCE "
        f"CROP.{FITS}"
    )


@pytest.mark.parametrize("worn", [True, False])
def test_a_single_product_photo_on_a_person_gives_only_the_product(worn):
    spec = single_bottom_detail_photo()
    if not worn:
        spec.products[0].refs[0].presentation = "flat"
    text = _text(build_prompt(spec)[1])
    primary = next(line for line in text.splitlines() if "camera angle" in line)
    only = "angle. T" + ONLY.format("straight jeans")[1:] + ". Its size"
    never = "angle. Never copy any person, body or face from it. Its size"
    assert (only in primary) is worn
    assert (never in primary) is not worn
