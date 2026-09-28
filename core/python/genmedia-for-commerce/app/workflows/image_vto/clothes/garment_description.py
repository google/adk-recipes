# Copyright 2025 Google LLC
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

"""Garment description for Image VTO.

Describes each garment image (brand, type, color, material, logos/details)
to improve generation accuracy and evaluation. Uses Gemini Flash with
EXTERIOR/INTERIOR detail classification.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from workflows.shared.gemini import generate_gemini
from workflows.shared.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)


def describe_garment_for_vto(
    client,
    garment_image: bytes | str,
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> dict:
    """Describe a single garment image for VTO purposes.

    Extracts brand, type, color, material, and all visible logos/text/marks
    with EXTERIOR/INTERIOR classification.

    Args:
        client: Gemini client instance
        garment_image: Garment image as bytes or GCS URI
        model: Gemini model to use

    Returns:
        dict: {"general": "...", "details": "..."} where:
            - general: Brand, product type, color, material
            - details: EXTERIOR/INTERIOR detail list of all visible logos/text/marks

    """
    system_prompt = """You are a fashion product analyst. You will receive a single product image.

Your tasks:
1. **Identify** the brand, product type, model name (if identifiable), material, and color scheme.
2. **Describe ALL visible text, logos, prints, and brand marks** on the product.

For each text/logo found, describe its content, color, and exact position (e.g. 'Silver SCOTT text on lower front of LEFT leg').
Systematically check EVERY part of the garment: left leg, right leg, left strap, right strap, waistband, front panel, back panel, chest, sleeves, etc.
If the same text appears on both sides, list it for BOTH sides.

Classify each detail as either EXTERIOR or INTERIOR:
- EXTERIOR: Logos, prints, or brand marks that are part of the garment's outward-facing design (e.g. printed text, embroidered logos, sewn-on patches). These are visible when the garment is worn.
- INTERIOR: Small fabric tags sewn on the inside of the garment (e.g. brand tags inside the neckline, care labels, size tags). These are only visible in flat-lay photos and would be hidden when worn.

Format each detail with its type, e.g. '[EXTERIOR] Silver SCOTT text on lower front of LEFT leg.' or '[INTERIOR] Small brand tag at inner back neckline.'

IMPORTANT: Only include details you can clearly identify with confidence. Do NOT include:
- Tiny marks, dots, or specks that could be image artifacts, compression noise, or fabric texture
- Details you cannot read or identify clearly
- Extremely small or ambiguous marks near edges/seams that might just be stitching

Return a JSON object:
{
  "general": "Brand name, product type, model name (if identifiable), material, color scheme",
  "details": "List of [EXTERIOR]/[INTERIOR] details, one per line"
}

Only return the JSON object, nothing else."""

    config = get_generate_content_config(
        temperature=0,
        thinking_budget=0,
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        response_text = generate_gemini(
            text_images_pieces=[
                garment_image,
                "Describe this garment in detail.",
            ],
            client=client,
            config=config,
            model=model,
        )

        result = json.loads(response_text)
        description = {
            "general": result.get("general", ""),
            "details": result.get("details", ""),
        }
        logger.debug(f"[VTO] Garment description: {description}")
        return description

    except Exception as e:
        logger.error(f"[VTO] Error describing garment: {e}")
        return {"general": "", "details": ""}


def describe_all_garments(
    client,
    garment_images: list[bytes | str],
    model: str = os.getenv("MODEL_NAME_GENERATED_4"),
) -> list[dict]:
    """Describe all garment images in parallel.

    Args:
        client: Gemini client instance
        garment_images: List of garment images as bytes or GCS URIs
        model: Gemini model to use

    Returns:
        list[dict]: List of {"general": "...", "details": "..."} for each garment

    """
    if not garment_images:
        return []

    with ThreadPoolExecutor(max_workers=len(garment_images)) as executor:
        futures = [
            executor.submit(describe_garment_for_vto, client, img, model)
            for img in garment_images
        ]
        return [f.result() for f in futures]
