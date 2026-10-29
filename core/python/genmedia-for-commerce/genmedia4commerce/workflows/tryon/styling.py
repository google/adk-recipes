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

"""How each product is worn, resolved in code for the drawing prompt.

The code knows how every product photo was taken (on a person, on a
mannequin: a worn photo whose person became one, ``worn``, flat, or cut
from the look photo), what the styling plan decided and which other
products are supplied, so the drawing prompt states one final instruction
per product (``product_lines``) and no rule for the image model to work out;
the judge gets the same instructions (``instructions``). Priority: the brand
styling rules (free text: the outfit plan applies them, and the prompt adds
them as they are), a crop of the look photo (worn exactly as there), the
plan (``outfit.analyze_outfit``: the outfit rules), then the defaults, only
without brand styling rules: outerwear worn as its product photo shows it
(closed stays closed), also over another supplied top; other tops
untucked. A product's own photo on a person was shot with other clothes:
it counts as a flat photo for tuck and layering and shows its closure and
fit.
"""

import re

from genmedia4commerce.core.types import ProductRef
from genmedia4commerce.workflows.tryon.framing import shows_feet
from genmedia4commerce.workflows.tryon.types import PromptProduct, PromptSpec

__all__ = [
    "FIT",
    "FIT_MANNEQUIN",
    "NO_FOOTWEAR",
    "instructions",
    "kind",
    "name",
    "product_lines",
    "ref_label",
]

NO_FOOTWEAR = (
    "No footwear is supplied: the wearer wears plain shoes that match the "
    "outfit."
)

_KIND = {
    "WORN": "photo on a person",
    "CROPPED FROM REFERENCE": "photo on another person",
    "FLAT": "flat photo",
}
# A worn photo, or a crop of one, whose person became a mannequin.
_ON_MANNEQUIN = "photo on a mannequin"
# A product's own photo on a person (or on a mannequin) shows how it fits.
FIT = "its photo on a person shows its fit"
FIT_MANNEQUIN = "its photo on a mannequin shows its fit"
# Outerwear without a plan: the closure its product photo shows.
_AS_PHOTOGRAPHED = "as its product photo shows it (closed stays closed)"
# A garment the plan wears closed: closed the way its photo shows it.
_AS_SHOWN = "exactly as in its product photo"
_OUTERWEAR_WORDS = re.compile(r"(jacket|blazer|coat|cardigan)s?\b", re.I)
# Outerwear is worn, never draped over the shoulders (1 in 3 denim images
# came out draped); a sleeveless one has nothing to put the arms through.
SLEEVES = "arms through its sleeves"
_SLEEVELESS = re.compile(r"vest|gilet|waistcoat|cape|poncho|sleeveless", re.I)
# Casual outer layers, worn open over a supplied top.
_CASUAL_WORDS = re.compile(
    r"field|denim|jean|chore|utility|work|overshirt|shacket|cardigan|bomber",
    re.I,
)
_UPPER = ("top", "full_body_outer", "dress")  # layered on the upper body
_PLAN_FIELDS = ("tuck", "closure", "layer_order", "hem", "visibility")

Plan = dict[str, dict]  # product id -> plan item


def ref_label(ref: ProductRef) -> str:
    """Return WORN, CROPPED FROM REFERENCE or FLAT for a garment image."""
    if ref.source == "model_crop":
        return "CROPPED FROM REFERENCE"
    return "WORN" if ref.presentation == "worn" else "FLAT"


def kind(ref: ProductRef) -> str:
    """How the photo was taken, in words: 'photo on a person', ...; 'photo
    on a mannequin' for one whose person became a mannequin."""
    return _ON_MANNEQUIN if ref.mannequin else _KIND[ref_label(ref)]


def name(product: PromptProduct) -> str:
    """The classifier's short name of the product, without a full stop."""
    return product.description.strip().rstrip(".")


def _the(product: PromptProduct) -> str:
    """'the field jacket (C)'; 'product C' when it has no name."""
    n = name(product)
    return f"the {n} ({product.id})" if n else f"product {product.id}"


def _both(products: list[PromptProduct]) -> str:
    return " and ".join(_the(p) for p in products)


def _applies(value) -> bool:
    """A plan value that is set and not 'n/a'."""
    text = str(value or "").strip()
    return bool(text) and not text.lower().startswith("n/a")


def _clean(value) -> str:
    return str(value).strip().rstrip(".")


def _is_outerwear(product: PromptProduct) -> bool:
    """``full_body_outer``, or a top named a jacket, blazer, coat or
    cardigan."""
    return product.category == "full_body_outer" or bool(
        product.category == "top"
        and _OUTERWEAR_WORDS.search(product.description)
    )


def _crop(product: PromptProduct, spec: PromptSpec) -> str | None:
    """The crop of the look photo that shows how ``product`` is worn: its
    REFERENCE CROP, or its own image when that is the crop (no upload);
    None without one."""
    crops = sum(product.id in ids for _, ids in spec.crops)
    if crops:
        return "its REFERENCE CROPS" if crops > 1 else "its REFERENCE CROP"
    for r in product.refs:
        if ref_label(r) == "CROPPED FROM REFERENCE":
            return f"its {kind(r)}"  # on another person, or a mannequin
    return None


def _layer(product: PromptProduct, spec: PromptSpec, plan: Plan) -> str:
    """'the outermost layer' or 'under the shirt (A)', from the plan's
    layer order of the upper-body products (two or more)."""
    layers = sorted(
        (plan[p.id]["layer_order"], k, p)
        for k, p in enumerate(spec.products)
        if p.category in _UPPER
        and isinstance(plan.get(p.id, {}).get("layer_order"), int)
    )
    order = [p for *_, p in layers]
    ids = [p.id for p in order]
    if len(order) < 2 or product.id not in ids:
        return ""
    i = ids.index(product.id)
    return "the outermost layer" if i == 0 else f"under {_the(order[i - 1])}"


def _planned(
    product: PromptProduct, spec: PromptSpec, plan: Plan
) -> tuple[str, str, list[str]]:
    """(how it is worn, its layer, the rest) from its plan item."""
    item = plan[product.id]
    how = [_clean(item.get(k)) for k in ("tuck", "closure")]
    guided = bool((spec.style_guidelines or "").strip())
    if how[1] == "fully closed" and not guided and not _crop(product, spec):
        how[1] = f"fully closed, {_AS_SHOWN}"  # not wider open than shown
    rest = [_clean(item.get(k)) for k in ("hem", "visibility")]
    return (
        ", ".join(x for x in how if _applies(x)),
        _layer(product, spec, plan),
        [x for x in rest if _applies(x)],
    )


def _defaults(
    product: PromptProduct, spec: PromptSpec
) -> tuple[str, str, list[str]]:
    """(how it is worn, its layer, the rest) without a plan: a casual outer
    layer over a supplied top is worn open, so the top shows; any other
    outerwear as its product photo shows it; other tops are untucked."""
    outer = [p for p in spec.products if _is_outerwear(p)]
    tops = [p for p in spec.products if p.category == "top" and p not in outer]
    casual = [p for p in outer if _CASUAL_WORDS.search(p.description)]
    if product in outer:
        over = f"over {_both(tops)}" if tops else ""
        if tops and product in casual:
            return "open (unzipped or unbuttoned)", over, []
        return _AS_PHOTOGRAPHED, over, []
    if product not in tops:
        return "", "", []
    front = ["its front shows between the open fronts"] if casual else []
    return "untucked", f"under {_both(outer)}" if outer else "", front


def _join(how: str, layer: str, rest: list[str], prefix: str) -> str:
    first = ", ".join(x for x in (f"{prefix}{how}" if how else "", layer) if x)
    return "; ".join(x for x in (first, *rest) if x)


def _instruction(product: PromptProduct, spec: PromptSpec, plan: Plan) -> str:
    """How ``product`` is worn: as its crop of the look photo shows (with
    what the plan read from it), else as planned, else by the defaults,
    plus its fit when it has a photo on a person; outerwear with sleeves is
    worn with the arms through them (``SLEEVES``). With brand styling rules
    the plan comes first (it applied them) and no default nor ``SLEEVES``
    is used (the rules decide)."""
    item = plan.get(product.id, {})
    planned = any(item.get(k) not in (None, "") for k in _PLAN_FIELDS)
    fields = _planned(product, spec, plan) if planned else ("", "", [])
    brand = bool((spec.style_guidelines or "").strip())
    crop = _crop(product, spec)
    if crop and brand and (worn := _join(*fields, prefix="worn ")):
        return f"{worn}; everything else exactly as in {crop}"
    if crop:
        details = _join(*fields, prefix="")
        return f"worn exactly as in {crop}" + (
            f": {details}" if details else ""
        )
    if not planned and not brand:
        fields = _defaults(product, spec)
    sleeved = not _SLEEVELESS.search(product.description)
    if not brand and _is_outerwear(product) and sleeved:
        fields = (*fields[:2], [SLEEVES, *fields[2]])
    how = _join(*fields, prefix="worn ")
    on_body = [r for r in product.refs if ref_label(r) == "WORN"]
    if on_body:
        fit = FIT_MANNEQUIN if all(r.mannequin for r in on_body) else FIT
        return "; ".join(x for x in (how, fit) if x)
    return how


def instructions(spec: PromptSpec) -> dict[str, str]:
    """How each product is worn, by product id ('' when nothing applies):
    the text after 'B — carpenter trousers: ' in PRODUCTS, also given to
    the judge (``evaluate``)."""
    items = spec.composition_plan.items if spec.composition_plan else []
    plan = {str(i.get("product_id")): i for i in items}
    return {p.id: _instruction(p, spec, plan) for p in spec.products}


def product_lines(spec: PromptSpec) -> list[str]:
    """One resolved line per product ('B — carpenter trousers: ...'), and
    plain shoes when the framing shows feet and none is supplied."""
    how = instructions(spec)
    lines = []
    for p in spec.products:
        head = " — ".join(x for x in (p.id, name(p)) if x)
        lines.append(f"{head}: {how[p.id]}." if how[p.id] else f"{head}.")
    footwear = any(p.category == "footwear" for p in spec.products)
    if shows_feet(spec) and not footwear:
        lines.append(NO_FOOTWEAR)
    return lines
