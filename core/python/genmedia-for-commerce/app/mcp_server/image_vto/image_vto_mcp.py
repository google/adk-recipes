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

"""Unified MCP wrapper for image virtual try-on (clothes + glasses)."""

import logging

from mcp_server.image_vto.clothes.clothes_mcp import run_image_vto_clothes
from mcp_server.image_vto.glasses.glasses_mcp import run_glasses_vto_tool

logger = logging.getLogger(__name__)


async def run_image_vto(
    person_image_base64: str,
    product_images_base64: list[str],
    is_glasses: bool = False,
    scenario: str = "a plain light grey studio environment",
    num_variations: int = 3,
    face_image_base64: str = "",
) -> dict:
    """Route to clothes or glasses VTO pipeline based on is_glasses flag.

    Args:
        person_image_base64: Base64-encoded image of the person.
            Full body for clothes, front face for glasses.
        product_images_base64: Base64-encoded product images (garments or glasses).
        is_glasses: True for glasses try-on, False for clothes try-on.
        scenario: Scene/setting description (clothes only). Default: light grey studio.
        num_variations: Number of variations to generate. Default: 3.
        face_image_base64: Optional separate face image for better preservation (clothes only).

    Returns:
        Dictionary with best result including image_base64, scores, and evaluation.

    """
    if is_glasses:
        logger.info(
            f"[image_vto] Routing to glasses pipeline ({len(product_images_base64)} images)"
        )
        return await run_glasses_vto_tool(
            model_image_base64=person_image_base64,
            product_images_base64=product_images_base64,
            num_variations=num_variations,
        )
    else:
        logger.info(
            f"[image_vto] Routing to clothes pipeline ({len(product_images_base64)} garments)"
        )
        return await run_image_vto_clothes(
            full_body_image_base64=person_image_base64,
            garment_images_base64=product_images_base64,
            scenario=scenario,
            num_variations=num_variations,
            face_image_base64=face_image_base64,
        )
