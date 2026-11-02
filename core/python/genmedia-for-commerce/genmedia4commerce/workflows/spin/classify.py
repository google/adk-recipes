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

"""Classify the spin input: product type, per-image view/rank, description.

One judge-model call (ported from the R2V ``classify_product_images`` prompt,
with the product description merged into the same call). Views say where
the product's front points in the picture, as the shoe views say where the
toe points: "the product's own right" made back views swap left and right
(and a phone's right is not a car's), and the frame labels of the spin
check (``product_check``) use the same words.
"""

import logging
from collections.abc import Sequence

from genmedia4commerce.core import gen
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.workflows.spin.types import (
    ProductType,
    SpinClassification,
)

__all__ = ["DEFAULT_DESCRIPTION", "VIEWS", "classify"]

logger = logging.getLogger(__name__)

# The angles around the product, in order (left/right = where its front
# points in the picture), then "other" for top, bottom and detail shots.
VIEWS = (
    "front",
    "front_right",
    "right",
    "back_right",
    "back",
    "back_left",
    "left",
    "front_left",
    "other",
)
DEFAULT_DESCRIPTION = "the product shown in the reference images"
UNRANKED = 99  # a missing or invalid rank: after the ranked images
# The call answers in about 5 s; a stuck one (once hung 120 s) is asked
# again once by ``core.gen``.
CLASSIFY_TIMEOUT_S = 40

_SYSTEM = """You are a product image analyst. You will receive {n} image(s) of the SAME product from different angles.

Your tasks:

1. **Product type** — Classify the product into one of:
   - "shoes": footwear (sneakers, boots, sandals, heels, etc.)
   - "other": everything else (vehicles, clothing, electronics, bags, furniture, accessories, etc.)

2. **For EACH image**, classify:

   a. **View** — Where the product's FRONT points in the picture (the front is the side with its main design, logo or opening; for a vehicle, its nose):
      - "front": the front faces the camera straight on.
      - "back": the front faces away from the camera (you see the back).
      - "right": side view, the front points to the RIGHT edge of the picture.
      - "left": side view, the front points to the LEFT edge of the picture.
      - "front_right": three-quarter, the front faces the camera and points to the right; the front and a side are both visible.
      - "front_left": three-quarter, the front faces the camera and points to the left.
      - "back_right": three-quarter, the front faces away and points to the right; the back and a side are both visible.
      - "back_left": three-quarter, the front faces away and points to the left.
      - "other": top-down, bottom/sole, detail or close-up shot, or any view that shows none of the above.
      Compare the images with each other: the same product details (buttons, a camera, a logo, a handle, a pocket) seen from both sides tell which way the front points.

   b. **Rank** — Compare the images that show the SAME view and order them as references, best first: rank 1 is the sharpest, best lit and most completely shown, then 2, 3, ... An image that is the only one of its view gets rank 1.

   c. **Presentation** — How the product is photographed:
      - "flat": no person (packshot, flat lay, ghost mannequin, product on a stand).
      - "worn": the product is worn or held by a person.

3. **Description** — A very short description of the product: only the type of product and its primary color (e.g. "a red ceramic mug", "a silver smartwatch"). Avoid describing the product with the brand, even if it is clearly visible. If the images show a pair or a set of the same item (earrings, gloves, cufflinks), describe ONE of them ("a gold hoop earring"): the spin shows a single item.

Return a JSON object:
{{
  "product_type": "shoes|other",
  "description": "a red ceramic mug",
  "classifications": [
    {{"index": 0, "view": "right", "rank": 1, "presentation": "flat"}},
    {{"index": 1, "view": "front", "rank": 1, "presentation": "worn"}},
    ...
  ]
}}

The "classifications" array must have exactly {n} entries, one per image, in order.
Only return the JSON object, nothing else."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "product_type": {"type": "string", "enum": ["shoes", "other"]},
        "description": {"type": "string"},
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "view": {"type": "string", "enum": list(VIEWS)},
                    "rank": {"type": "integer"},
                    "presentation": {
                        "type": "string",
                        "enum": ["flat", "worn"],
                    },
                },
                "required": ["index", "view", "rank", "presentation"],
            },
        },
    },
    "required": ["product_type", "description", "classifications"],
}


def _rank(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return UNRANKED


def _views(entries: object, n: int) -> list[dict]:
    """One ``{index, view, rank, presentation}`` per image; missing/invalid -> defaults."""
    by_index: dict[int, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        index = entry.get("index") if isinstance(entry, dict) else None
        if isinstance(index, int) and 0 <= index < n and index not in by_index:
            by_index[index] = entry
    views = []
    for i in range(n):
        entry = by_index.get(i, {})
        view = entry.get("view")
        views.append(
            {
                "index": i,
                "view": view if view in VIEWS else "other",
                "rank": _rank(entry.get("rank")),
                "presentation": "worn"
                if entry.get("presentation") == "worn"
                else "flat",
            }
        )
    return views


def _ask(parts: list, n: int) -> dict:
    """The classification call (``core.gen`` asks a stalled call again)."""
    result = gen.generate_json(
        parts,
        system=_SYSTEM.format(n=n),
        schema=_SCHEMA,
        params=gen.TextParams(
            model=get_models().judge,
            thinking="MEDIUM",
            deadline_s=CLASSIFY_TIMEOUT_S,
        ),
    )
    return result if isinstance(result, dict) else {}


def classify(images: Sequence[bytes]) -> SpinClassification:
    """Classify the product type and the view/rank of every image."""
    if not images:
        raise InputError("classify needs at least one image")
    n = len(images)
    parts: list[gen.Part] = []
    for i, image in enumerate(images):
        parts += [f"IMAGE_{i}:", image]
    parts.append(
        f"Classify the product type and the view and rank of all {n} images."
    )
    result = _ask(parts, n)
    raw_type = result.get("product_type")
    product_type: ProductType = "shoes" if raw_type == "shoes" else "other"
    description = (
        str(result.get("description") or "").strip() or DEFAULT_DESCRIPTION
    )
    views = _views(result.get("classifications"), n)
    logger.info(
        f"spin classify: {product_type} ({description}); "
        + ", ".join(
            f"{v['index']}={v['view']}(r{v['rank']}, {v['presentation']})"
            for v in views
        )
    )
    return SpinClassification(product_type, views, description)
