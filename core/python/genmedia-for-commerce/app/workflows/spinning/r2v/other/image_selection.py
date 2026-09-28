# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Image selection for generic product R2V spinning.

Uses Gemini to classify product type and view angles in one call,
then selects the best 4 images for VEO reference image generation.

Layout depends on product type (matching stack_and_canvas_images):
  - Shoes/Cars (3D objects): sides solo (pos 1-2), front+back stacked (pos 3-4)
  - Other (flat objects): front+back solo (pos 1-2), sides stacked (pos 3-4)
"""

import json
import logging
import os

from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)

VALID_VIEWS = ["right", "left", "front", "back", "other"]
VALID_PRODUCT_TYPES = ["shoes", "cars", "other"]

# Products where side views are most informative (3D objects)
SIDE_PRIORITY_PRODUCTS = {"shoes", "cars"}


def classify_product_images(
    client,
    images_bytes: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_1"),
) -> dict:
    """Classify product type and each image's view angle in a single Gemini call.

    Returns:
        dict: {
            "product_type": "shoes" | "cars" | "other",
            "classifications": [{"index": 0, "view": "right", "quality": 8}, ...]
        }

    """
    num_images = len(images_bytes)

    system_prompt = f"""You are a product image analyst. You will receive {num_images} image(s) of the SAME product from different angles.

Your tasks:

1. **Product type** — Classify the product into one of:
   - "shoes": footwear (sneakers, boots, sandals, heels, etc.)
   - "cars": vehicles (cars, motorcycles, bicycles, etc.)
   - "other": everything else (clothing, electronics, bags, furniture, accessories, etc.)

2. **For EACH image**, classify:

   a. **View** — Which side of the product is shown:
      - "right": The right side of the product is the dominant view.
      - "left": The left side of the product is the dominant view.
      - "front": The front of the product faces the camera.
      - "back": The back/rear of the product faces the camera.
      - "other": Top-down, bottom/sole, detail shot, or any view that doesn't clearly show one of the four main sides.

   b. **Quality** — Rate 1-10:
      - 10: Sharp, well-lit, clean background, product fully visible
      - 5: Acceptable but some issues (slight blur, busy background, partial crop)
      - 1: Poor quality, heavily cropped, or unusable

Return a JSON object:
{{
  "product_type": "shoes|cars|other",
  "classifications": [
    {{"index": 0, "view": "right", "quality": 8}},
    {{"index": 1, "view": "front", "quality": 9}},
    ...
  ]
}}

The "classifications" array must have exactly {num_images} entries, one per image, in order.
Only return the JSON object, nothing else."""

    pieces: list = []
    for i, img in enumerate(images_bytes):
        pieces.append(f"IMAGE_{i}:")
        pieces.append(img)
    pieces.append(
        f"Classify the product type and view angle/quality of all {num_images} images."
    )

    config = get_generate_content_config(
        temperature=0,
        thinking_budget=0,
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=pieces,
            client=client,
            config=config,
            model=model,
        )
        result = json.loads(response_text)

        # Validate product type
        product_type = result.get("product_type", "other")
        if product_type not in VALID_PRODUCT_TYPES:
            product_type = "other"

        # Validate classifications
        classifications = result.get("classifications", [])
        validated = []
        for i in range(num_images):
            if i < len(classifications):
                entry = classifications[i]
                view = entry.get("view", "other")
                if view not in VALID_VIEWS:
                    view = "other"
                quality = entry.get("quality", 5)
                if not isinstance(quality, (int, float)):
                    quality = 5
                validated.append(
                    {"index": i, "view": view, "quality": int(quality)}
                )
            else:
                validated.append({"index": i, "view": "other", "quality": 5})

        return {"product_type": product_type, "classifications": validated}

    except Exception as e:
        logger.error(f"Error classifying product images: {e}")
        return {
            "product_type": "other",
            "classifications": [
                {"index": i, "view": "other", "quality": 5}
                for i in range(num_images)
            ],
        }


def select_best_images(
    client,
    images_bytes: list[bytes],
    model: str = os.getenv("MODEL_NAME_GENERATED_1"),
) -> list[bytes]:
    """Select the best 4 images for R2V spinning from any number of inputs.

    Classifies product type and views in one call, then selects based on type:

    Shoes/Cars (3D — side views are most informative):
      - Position 1 (solo canvas): best right-side image
      - Position 2 (solo canvas): best left-side image
      - Position 3 (stacked with 4): best front image
      - Position 4 (stacked with 3): best back image

    Other (flat — front/back views are most informative):
      - Position 1 (solo canvas): best front image
      - Position 2 (solo canvas): best back image
      - Position 3 (stacked with 4): best right-side image
      - Position 4 (stacked with 3): best left-side image

    Falls back to best available views if not all 4 sides are covered.
    If 4 or fewer images, returns them as-is (no selection needed).

    Returns:
        list[bytes]: Up to 4 images ordered for stack_and_canvas_images

    """
    if len(images_bytes) <= 4:
        return images_bytes

    result = classify_product_images(client, images_bytes, model)
    product_type = result["product_type"]
    classifications = result["classifications"]

    logger.info(
        f"[R2V Selection] Product type: {product_type}, classified {len(classifications)} images: "
        + ", ".join(
            f"IMG_{c['index']}={c['view']}(q{c['quality']})"
            for c in classifications
        )
    )

    # Group by view, sorted by quality descending within each group
    by_view: dict[str, list[dict]] = {}
    for c in classifications:
        by_view.setdefault(c["view"], []).append(c)
    for view in by_view:  # noqa: PLC0206
        by_view[view].sort(key=lambda x: x["quality"], reverse=True)

    # Slot priorities depend on product type
    if product_type in SIDE_PRIORITY_PRODUCTS:
        # 3D objects: sides solo, front+back stacked
        slot_priorities = [
            [
                "right",
                "front",
                "back",
                "left",
                "other",
            ],  # slot 1 (solo): right side
            [
                "left",
                "back",
                "front",
                "right",
                "other",
            ],  # slot 2 (solo): left side
            [
                "front",
                "right",
                "left",
                "other",
                "back",
            ],  # slot 3 (stacked): front
            [
                "back",
                "left",
                "right",
                "other",
                "front",
            ],  # slot 4 (stacked): back
        ]
    else:
        # Flat objects: front+back solo, sides stacked
        slot_priorities = [
            ["front", "right", "left", "back", "other"],  # slot 1 (solo): front
            ["back", "left", "right", "front", "other"],  # slot 2 (solo): back
            [
                "right",
                "front",
                "back",
                "left",
                "other",
            ],  # slot 3 (stacked): right side
            [
                "left",
                "back",
                "front",
                "right",
                "other",
            ],  # slot 4 (stacked): left side
        ]

    selected: list[bytes] = []
    used_indices: set[int] = set()

    for slot_views in slot_priorities:
        best = None
        for view in slot_views:
            candidates = by_view.get(view, [])
            for candidate in candidates:
                if candidate["index"] not in used_indices:
                    best = candidate
                    break
            if best:
                break

        if best:
            selected.append(images_bytes[best["index"]])
            used_indices.add(best["index"])
            logger.info(
                f"[R2V Selection] Slot {len(selected)}: IMG_{best['index']} "
                f"view={best['view']} quality={best['quality']}"
            )

    logger.info(
        f"[R2V Selection] Selected {len(selected)} from {len(images_bytes)} images"
    )
    return selected
