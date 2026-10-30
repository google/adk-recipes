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

"""Outfit route (D16): garment choice and plan (the reference model:
``reference``; the drawing's images: ``cards``)."""

import dataclasses
import logging
from collections.abc import Iterable, Sequence

from genmedia4commerce.core import degraded, gen
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.tryon.styling import ref_label
from genmedia4commerce.workflows.tryon.types import CompositionPlan

__all__ = [
    "analyze_outfit",
    "best_upload",
    "choose_garment_images",
    "extra_crops",
    "mannequin_crops",
    "uploads_only",
]

logger = logging.getLogger(__name__)


_TUCKS = ("tucked", "untucked", "front-tucked", "n/a")
_CLOSURES = (
    "fully closed",
    "fully open",
    "top button(s) closed, bottom open",
    "bottom button(s) closed, top open",
    "n/a — no front closure",
)

_PLAN_SYSTEM = f"""You are a fashion stylist. You receive the products of ONE outfit, one image each, labelled PRODUCT A, PRODUCT B, ..., and possibly extra images labelled CROPPED FROM REFERENCE, PRODUCT X, showing those products worn by another person, and possibly BRAND STYLING RULES. Decide how the wearer wears the WHOLE outfit. Your plan is AUTHORITATIVE for the image generator. Only the listed products exist: never mention anything else a person in the images wears.

Each image is one of three kinds, with its own role:
- CROPPED FROM REFERENCE: cut from the look photo, the whole outfit styled on a model. It is the styling authority for the products it shows: copy their tuck, closure, layering, length, hem, break and visibility from it, over the outfit rules.
- WORN: the product's own photo on a person, shot with other clothes. For tuck and layering it counts as a FLAT image: this outfit decides them. Its closure is the product's, like a FLAT image's (the closure rule below). Otherwise it is used for FIT ONLY: length, break, turn-up or cuff, width at the bottom, whether trousers go over or into boots, and how it hangs.
- FLAT: a product photo with no person. It shows how the product looks (closures, hem, pockets); its styling comes from the rules below.

PRIORITY, highest first: 1. the BRAND STYLING RULES, when given: they apply to every product, whatever its image; where a rule conflicts with an image, the rule wins. 2. A CROPPED FROM REFERENCE image. 3. The outfit rules below: the closure rule and the tuck by the outfit's formality and layers. 4. The fit a WORN image shows (the hem). 5. The defaults below. Tuck is decided by the outfit, never taken from a WORN image.

For EACH product decide:
1. tuck, one of: {", ".join(_TUCKS)}.
   - Copy the tuck state a CROPPED FROM REFERENCE image shows.
   - Every other product, including one with a WORN image: decide by formality and layering. A button-down dress shirt under a tailored matching-fabric suit is ALWAYS tucked. A dress shirt under a non-matching jacket or worn alone is usually tucked with tailored trousers or chinos; untucked only if it is cut as a casual untucked shirt (boxy, square hem) and the trousers are casual.
   - Knits (sweaters, jumpers, cardigans, pullovers) over trousers or jeans are untucked: the hem hangs in one continuous line, never half- or French-tucked, even with a hand in a pocket.
   - A polo or t-shirt worn alone is untucked unless the outfit is clearly tailored or formal.
   - Under a vest or waistcoat the shirt follows the rules above. The outermost jacket, coat or blazer, every bottom, shoe and accessory: n/a.
2. closure, one of: {"; ".join(_CLOSURES)}.
   - Copy the closure state a CROPPED FROM REFERENCE image shows.
   - Closure: tailored jackets and coats (blazers, suit jackets, tailored or belted coats) are worn closed exactly as their product image shows them (a tied belt stays tied). Casual outer layers (field, denim, chore and utility jackets, overshirts, cardigans) worn over another supplied top are worn fully open, so that top's front shows. Every other garment is worn closed or open exactly as its product image (FLAT or WORN) shows it. A CROPPED FROM REFERENCE image or the BRAND STYLING RULES decide first.
   - A jacket or blazer worn closed is fastened as in its product image (the same buttons fastened).
   - Pullovers, t-shirts, bottoms, shoes and accessories: n/a — no front closure.
3. layer_order: integer, 1 = outermost upper-body layer, increasing inwards; bottoms, shoes and accessories come after every upper-body layer.
4. hem, for bottoms, dresses and skirts: one short phrase with the length, the turn-up or cuff, the break and where the hem falls ("full length, single turn-up, the hem falls over the boot shafts", "cropped above the ankle, no break", "knee length"). Copy it from a CROPPED FROM REFERENCE image. Without one, a WORN image gives it: its length, break, turn-up or cuff, width at the bottom and whether it goes over or into boots. Tall boots: when the outfit has tall boots and full-length trousers whose image does not show them tucked in, the hem falls over the boot shafts and only the feet show. Every other product: n/a.
5. visibility: one short phrase saying how much of the product shows on the wearer, in concrete anatomical words ("fully visible", "only the collar visible at the neckline", "lower legs visible below the coat", "toe caps visible under a full trouser break", "only the feet visible under the trouser hem"). Read it from CROPPED FROM REFERENCE images: a product barely visible there stays barely visible. For every other product it follows from this outfit's layers; when in doubt, choose more visible, within what its outer layer leaves uncovered. A product no other product covers is "fully visible". An inner layer under a closed outer layer shows only what the outer garment leaves uncovered in its own product image: its armholes, its neckline exactly as photographed, below its hem. Never write a visibility that needs the outer garment opened wider than its product image shows. Example: a blazer under a closed sleeveless wrap coat: "only its sleeves visible through the armholes". Under an open outer layer: "front visible between the open fronts". Tall boots under a trouser hem that falls over their shafts: "only the feet visible under the trouser hem".

Be decisive: one value per field, never alternatives. Return ONLY the JSON object:
{{"items": [{{"product_id": "A", "tuck": "...", "closure": "...", "layer_order": 1, "hem": "...", "visibility": "..."}}]}}"""


def uploads_only(product: Product) -> Product:
    """``product`` with its uploaded photos only (none: as it is)."""
    uploads = [r for r in product.refs if r.source == "upload"]
    return dataclasses.replace(product, refs=uploads) if uploads else product


def best_upload(product: Product) -> ProductRef:
    """The product's best upload: front views win, then worn before flat,
    then the better rank (a product with no upload: its best crop)."""
    uploads = uploads_only(product)
    best = uploads.best_ref("front") or uploads.best_ref()
    if best is None:  # every product has a photo
        raise ValueError(f"product {product.id} has no photo")
    return best


def choose_garment_images(
    classification: Classification,
) -> list[tuple[Product, ProductRef]]:
    """One garment image per product: its hero, its first image
    (``hero``); the reference crops not chosen go along as extra views
    (``extra_crops``)."""
    return [(p, p.refs[0]) for p in classification.products]


def extra_crops(
    products: Sequence[Product], shown: Iterable[bytes]
) -> list[tuple[bytes, list[str]]]:
    """Every reference crop not in ``shown``, once, with the ids of the
    products it shows (a band can show several, e.g. a blazer and a coat)."""
    skip = {id(image) for image in shown}
    crops: dict[int, tuple[bytes, list[str]]] = {}
    for product in products:
        for ref in product.refs:
            if ref.source == "model_crop" and id(ref.image) not in skip:
                crops.setdefault(id(ref.image), (ref.image, []))[1].append(
                    product.id
                )
    return list(crops.values())


def mannequin_crops(products: Sequence[Product]) -> list[bytes]:
    """The reference crops cut from a photo whose person became a
    mannequin (``worn``)."""
    return [
        r.image
        for p in products
        for r in p.refs
        if r.source == "model_crop" and r.mannequin
    ]


# The plan's shape, so the model writes valid JSON (``reference``).
_PLAN_KEYS = (
    "product_id",
    "tuck",
    "closure",
    "layer_order",
    "hem",
    "visibility",
)
_TEXT = {"type": "STRING"}
_PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "product_id": _TEXT,
                    "tuck": _TEXT,
                    "closure": _TEXT,
                    "layer_order": {"type": "INTEGER"},
                    "hem": _TEXT,
                    "visibility": _TEXT,
                },
                "required": list(_PLAN_KEYS),
                "propertyOrdering": list(_PLAN_KEYS),
            },
        }
    },
    "required": ["items"],
}


def _plan_item(product_id: str, raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    layer = raw.get("layer_order")
    return {
        "product_id": product_id,
        "tuck": raw.get("tuck") if raw.get("tuck") in _TUCKS else "",
        "closure": raw.get("closure")
        if raw.get("closure") in _CLOSURES
        else "",
        "layer_order": layer if isinstance(layer, int) else None,
        "hem": str(raw.get("hem") or ""),
        "visibility": str(raw.get("visibility") or ""),
    }


def analyze_outfit(
    chosen: list[tuple[Product, ProductRef]],
    crops: Sequence[tuple[bytes, list[str]]] = (),
    guidelines: str = "",
) -> CompositionPlan:
    """Decide tuck, closure, layering, hem and visibility for every product.

    One judge-model call at medium thinking: as fast as the text model at
    the median and without its spikes (21 vs 98 s on one outfit).

    ``crops`` (``extra_crops``) show products worn by the reference model;
    ``guidelines``, the brand styling rules, go to the plan as they are.
    Returns an empty plan when the call fails (the prompt then resolves
    the defaults, ``styling``).
    """
    parts: list[gen.Part] = []
    for product, ref in chosen:
        label = f"PRODUCT {product.id} ({ref_label(ref)}, {product.category}): "
        parts += [label + product.description, ref.image]
    for image, ids in crops:
        parts += [
            f"CROPPED FROM REFERENCE, PRODUCT {' and '.join(ids)}:",
            image,
        ]
    if guidelines.strip():
        parts.append(f"BRAND STYLING RULES: {guidelines.strip()}")
    parts.append("Return the styling plan JSON for this outfit.")
    try:
        raw = gen.generate_json(
            parts,
            system=_PLAN_SYSTEM,
            schema=_PLAN_SCHEMA,
            params=TextParams(model=get_models().judge, thinking="MEDIUM"),
        )
    except GenerationError as exc:
        logger.warning(f"analyze_outfit failed: {exc}")
        degraded.report("styling plan")
        return CompositionPlan()
    raw = raw if isinstance(raw, dict) else {}
    by_id = {
        str(item.get("product_id")): item
        for item in raw.get("items") or []
        if isinstance(item, dict)
    }
    return CompositionPlan(
        items=[_plan_item(p.id, by_id.get(p.id)) for p, _ in chosen],
    )
