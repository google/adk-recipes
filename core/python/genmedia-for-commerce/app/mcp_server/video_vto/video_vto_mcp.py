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

"""Unified MCP wrapper for video virtual try-on (clothes + glasses)."""

import logging

from mcp_server.video_vto.clothes.clothes_mcp import run_video_vto_clothes
from mcp_server.video_vto.glasses.glasses_mcp import run_glasses_video_generate

logger = logging.getLogger(__name__)


async def run_video_vto(
    person_image_base64: str,
    product_images_base64: list[str],
    is_glasses: bool = False,
    scenario: str = "a plain white studio background",
    num_variations: int = 3,
    face_image_base64: str = "",
    number_of_videos: int = 4,
    prompt: str = "",
) -> dict:
    """Route to clothes or glasses video VTO pipeline based on is_glasses flag.

    Args:
        person_image_base64: Base64-encoded image of the person.
            Full body for clothes, front face for glasses.
        product_images_base64: Base64-encoded product images (garments or glasses).
        is_glasses: True for glasses video, False for clothes video.
        scenario: Background description (clothes only). Default: white studio.
        num_variations: Image VTO variations before video (clothes only). Default: 3.
        face_image_base64: Optional face image for preservation (clothes only).
        number_of_videos: Number of videos to generate. Default: 4.
        prompt: Optional custom video generation prompt.

    Returns:
        Dictionary with videos (base64), scores, and metadata.

    """
    if is_glasses:
        logger.info(
            f"[video_vto] Routing to glasses pipeline ({len(product_images_base64)} images)"
        )
        # Glasses video expects a single product image string, not a list
        product_image = (
            product_images_base64[0] if product_images_base64 else ""
        )
        return await run_glasses_video_generate(
            model_image_base64=person_image_base64,
            product_image_base64=product_image,
            prompt=prompt,
            number_of_videos=number_of_videos,
        )
    else:
        logger.info(
            f"[video_vto] Routing to clothes pipeline ({len(product_images_base64)} garments)"
        )
        return await run_video_vto_clothes(
            full_body_image_base64=person_image_base64,
            garment_images_base64=product_images_base64,
            scenario=scenario,
            num_variations=num_variations,
            face_image_base64=face_image_base64,
            number_of_videos=number_of_videos,
            prompt=prompt,
        )
