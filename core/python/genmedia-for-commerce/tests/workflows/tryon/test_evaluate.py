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

"""Tests for the try-on evaluator (product checks faked)."""

import asyncio
import dataclasses

import pytest

from genmedia4commerce.core import media
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Category,
    Classification,
    Product,
    ProductRef,
    Verdict,
    quality_score,
)
from genmedia4commerce.eval import outfit
from genmedia4commerce.workflows.tryon.evaluate import evaluator
from genmedia4commerce.workflows.tryon.types import (
    CompositionPlan,
    PromptProduct,
    TryOnContext,
)

COVERED = "sleeves visible emerging from armholes"
IDS = ("A", "B", "C", "D", "E")
REFS = {pid: png(color=(i, i, i)) for i, pid in enumerate(IDS)}
VISIBILITY = {
    "A": COVERED,
    "B": "fully visible over torso and hips",
    "C": "Fully visible, carried on the shoulder",
    "D": "",
}


BODY = png(color=(7, 7, 7))


def _context(plan: CompositionPlan | None) -> TryOnContext:
    products = [
        Product(pid, "top", f"{pid} item", [ProductRef(REFS[pid], "front")])
        for pid in IDS
    ]
    return TryOnContext(
        classification=Classification("outfit", products, "full_body"),
        wearer_body=BODY,
        wearer_face=None,
        reference_face=None,
        style_guidelines="",
        scenario="",
        params=ImageParams(),
        plan=plan,
    )


def _drawn(ctx: TryOnContext) -> list[PromptProduct]:
    """The drawing's products: each with its first image."""
    return [
        PromptProduct(p.id, p.category, p.refs[:1], p.description)
        for p in ctx.classification.products
    ]


def _evaluate(ctx: TryOnContext):
    return evaluator(ctx, "front", _drawn(ctx))


@pytest.fixture
def product_calls(monkeypatch) -> dict:
    calls = {}

    def evaluate_outfit(products, target, *, wearer=None, face=None):
        for p in products:
            (pid,) = [k for k, ref in REFS.items() if p.reference == ref]
            calls[pid] = p.visibility
        return Verdict(publish=True)

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)
    return calls


def _visibilities(ctx: TryOnContext, calls: dict) -> list:
    verdict, _ = asyncio.run(_evaluate(ctx)(png()))
    assert verdict.publish
    return [calls[pid] for pid in IDS]


def test_only_covered_products_get_their_visibility(product_calls):
    items = [
        {"product_id": pid, "visibility": text}
        for pid, text in VISIBILITY.items()
    ]  # E has no plan item
    ctx = _context(CompositionPlan(items))
    assert _visibilities(ctx, product_calls) == [COVERED] + [None] * 4


def test_no_plan_no_visibility(product_calls):
    assert _visibilities(_context(None), product_calls) == [None] * 5


PHOTO, SKIRT, SHOES, BAND = (png(color=(9, 9, c)) for c in range(4))


def _cropped(pid: str, category: Category, crop: bytes) -> Product:
    """A product the look adds: its crop, then a band of the look."""
    refs = [
        ProductRef(image, "front", presentation="worn", source="model_crop")
        for image in (crop, BAND)
    ]
    return Product(pid, category, f"{category} item", refs)


def _judged(monkeypatch, products: list[Product]) -> list[tuple]:
    """(reference, category) of each product the judge is shown."""
    seen = []

    def evaluate_outfit(items, target, *, wearer=None, face=None):
        seen.extend((p.reference, p.product_filter) for p in items)
        return Verdict(publish=True)

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)
    ctx = _context(None)
    ctx.classification = Classification("outfit", products, "full_body")
    asyncio.run(_evaluate(ctx)(png()))
    return seen


def test_every_product_drawn_is_judged_on_the_image_the_drawing_got(
    monkeypatch,
):
    """An uploaded product on its photo, one the look adds on its crop (a
    coat drawn draped over the shoulders is caught on it)."""
    top = Product("A", "top", "top item", [ProductRef(PHOTO, "front")])
    products = [
        top,
        _cropped("B", "bottom", SKIRT),
        _cropped("C", "footwear", SHOES),
    ]
    assert _judged(monkeypatch, products) == [
        (PHOTO, "top"),
        (SKIRT, "bottom"),
        (SHOES, "footwear"),
    ]


def test_a_texture_only_mismatch_publishes_with_a_lower_score(fake_gen):
    """The image judge's answer rejects for a knit difference only: the
    image is published, one imperfection lower in score."""
    rib = {
        "feature": "fabric surface",
        "category": "fabric_texture",
        "kind": "differs",
        "explanation": "a ribbed knit instead of a smooth jersey",
    }
    fake_gen.json(
        {
            "products": [
                {"product": "PRODUCT_1", "name": "top", "defects": [rib]}
            ],
            "publish": False,
            "imperfection_count": 1,
            "description": "The top's knit differs.",
        }
    )
    verdict, _ = asyncio.run(_evaluate(_context(None))(png()))
    assert verdict.publish
    assert verdict.imperfection_count == 1
    assert verdict.details["score"] == quality_score(True, 1) == 85.0
    assert verdict.details["products"][0]["findings"] == [
        "fabric_texture: fabric surface — a ribbed knit instead of a smooth "
        "jersey"
    ]


FACE = png(color=(9,) * 3)


@pytest.mark.parametrize(
    ("view", "face", "seen_face"),
    [("front", FACE, FACE), ("front", None, None), ("back", FACE, None)],
    ids=["front", "front-no-face", "back"],
)
def test_only_the_front_judge_sees_the_wearer_and_the_face(
    monkeypatch, view, face, seen_face
):
    """The front view is drawn from the wearer, so it is judged against
    it and the face crop of the original photo (none when Vision found no
    face); the back view is drawn from the front result alone."""
    seen = []

    def evaluate_outfit(items, target, **kwargs):
        seen.append(kwargs)
        return Verdict(publish=True)

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)
    ctx = dataclasses.replace(_context(None), reference_face=face)
    asyncio.run(evaluator(ctx, view, _drawn(ctx))(png()))
    wearer = BODY if view == "front" else None
    assert seen == [{"wearer": wearer, "face": seen_face}]


def test_a_body_defect_rejects_the_image(fake_gen):
    """The judge sees the wearer and names a body defect: the image is
    rejected and its description says why."""
    fake_gen.json(
        {
            "products": [
                {"product": "PRODUCT_1", "name": "top", "defects": []}
            ],
            "publish": True,
            "imperfection_count": 0,
            "description": "The top is right.",
            "body_defects": ["the prosthetic leg is shown instead of covered"],
            "body_parts": ["prosthetic leg"],
        }
    )
    verdict, _ = asyncio.run(_evaluate(_context(None))(png()))
    assert not verdict.publish
    assert verdict.description.startswith(outfit.BODY_NOTE)
    assert verdict.details["score"] == quality_score(False, 0) == 70.0
    # Kept as a list for the fix pass (``fix_prompt``).
    assert verdict.details["body_defects"] == [
        "the prosthetic leg is shown instead of covered"
    ]
    assert verdict.details["body_parts"] == ["prosthetic leg"]


def test_the_judge_gets_the_face_next_to_the_wearer(fake_gen):
    """The real judge call: the face crop follows the WEARER, before the
    note that asks for the face to be the wearer's."""
    fake_gen.json(
        {
            "products": [],
            "publish": True,
            "imperfection_count": 0,
            "description": "ok",
            "body_defects": [],
        }
    )
    ctx = dataclasses.replace(_context(None), reference_face=FACE)
    asyncio.run(_evaluate(ctx)(png()))
    (call,) = fake_gen.calls
    parts = call["raw_parts"]
    k = parts.index(outfit.WEARER_LABEL)
    assert parts[k + 1 : k + 5] == [
        media.to_png(BODY),
        outfit.FACE_LABEL,
        media.to_png(FACE),
        outfit.WEARER_NOTE,
    ]
    assert "body_defects" in call["schema"]["required"]


def test_another_persons_face_is_a_body_defect(fake_gen):
    """The judge sees the catalogue model's face instead of the wearer's:
    the image is rejected, like any body defect, even with glasses among
    the products (the judge sees what the glasses leave visible)."""
    fake_gen.json(
        {
            "products": [],
            "publish": True,
            "imperfection_count": 0,
            "description": "The products are right.",
            "body_defects": [
                "the face is another person's instead of the WEARER's"
            ],
        }
    )
    ctx = dataclasses.replace(_context(None), reference_face=FACE)
    drawn = _drawn(ctx)
    drawn[-1] = dataclasses.replace(drawn[-1], category="eyewear")
    verdict, _ = asyncio.run(evaluator(ctx, "front", drawn)(png()))
    assert not verdict.publish
    assert verdict.description.startswith(
        f"{outfit.BODY_NOTE} the face is another person's"
    )
    assert "face_score" not in verdict.details
    assert verdict.details["score"] == quality_score(False, 0) == 70.0
