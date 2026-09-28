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

"""MCP tool wrappers for the glasses video VTO pipeline."""

import base64
import logging
import os

from fastapi.concurrency import run_in_threadpool
from google import genai
from workflows.video_vto.glasses.pipeline import (
    run_generation_pipeline,
)

logger = logging.getLogger(__name__)


def _get_clients():
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    genai_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    veo_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    nano_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return genai_client, veo_client, nano_client


async def run_glasses_video_generate(
    model_image_base64: str,
    product_image_base64: str = "",
    model_side_image_base64: str = "",
    prompt: str = "",
    number_of_videos: int = 4,
    background_color: str = "0,215,6,255",
    zoom_level: int = 0,
    is_animation_mode: bool = False,
) -> dict:
    """Generate glasses try-on videos from model and product images.

    Creates a collage from the provided images and generates videos using Veo.

    Args:
        model_image_base64: Base64-encoded model/face image.
        product_image_base64: Base64-encoded glasses product image. Optional.
        model_side_image_base64: Base64-encoded side view of model. Optional.
        prompt: Video generation prompt.
        number_of_videos: Number of videos to generate. Default: 4.
        background_color: Background color as "r,g,b,a". Default: "0,215,6,255".
        zoom_level: Zoom level 0-6. Default: 0.
        is_animation_mode: If true, animate model image directly. Default: false.

    Returns:
        Dictionary with videos (base64 list), filenames, and collage_data.

    """
    model_bytes = None
    product_bytes = None
    model_side_bytes = None

    if model_image_base64:
        try:
            model_bytes = base64.b64decode(model_image_base64)
        except Exception as e:
            return {"error": f"Invalid base64 for model image: {e}"}

    if product_image_base64:
        try:
            product_bytes = base64.b64decode(product_image_base64)
        except Exception as e:
            return {"error": f"Invalid base64 for product image: {e}"}

    if model_side_image_base64:
        try:
            model_side_bytes = base64.b64decode(model_side_image_base64)
        except Exception as e:
            return {"error": f"Invalid base64 for model side image: {e}"}

    try:
        bg_color = tuple(map(int, background_color.split(",")))
    except ValueError:
        bg_color = (0, 215, 6, 255)

    zoom_level = max(0, min(6, zoom_level))

    logger.info(
        f"[MCP glasses_video_generate] Starting: "
        f"number_of_videos={number_of_videos}, animation={is_animation_mode}"
    )

    result = await run_in_threadpool(
        run_generation_pipeline,
        prompt,
        number_of_videos,
        model_bytes,
        product_bytes,
        model_side_image_bytes=model_side_bytes,
        is_template_product_image=False,
        bg_color=bg_color,
        zoom_level=zoom_level,
        is_animation_mode=is_animation_mode,
    )

    logger.info(
        f"[MCP glasses_video_generate] Complete. {len(result.get('videos', []))} videos"
    )
    return result


async def run_glasses_video_regenerate(
    prompt: str,
    collage_data_base64: str,
    number_of_videos: int = 1,
    background_color: str = "0,215,6,255",
    is_animation_mode: bool = False,
) -> dict:
    """Regenerate glasses videos from an existing collage.

    Args:
        prompt: Video generation prompt.
        collage_data_base64: Base64-encoded collage image from a previous generation.
        number_of_videos: Number of videos to regenerate. Default: 1.
        background_color: Background color as "r,g,b,a". Default: "0,215,6,255".
        is_animation_mode: If true, skip post-processing. Default: false.

    Returns:
        Dictionary with videos (base64 list) and filenames.

    """
    from workflows.video_vto.glasses.pipeline import (
        RegenerationRequest,
        run_regeneration_pipeline,
    )

    req = RegenerationRequest(
        prompt=prompt,
        collage_data=collage_data_base64,
        number_of_videos=number_of_videos,
        bg_color=background_color,
        is_animation_mode=is_animation_mode,
    )

    logger.info(
        f"[MCP glasses_video_regenerate] Starting: {number_of_videos} videos"
    )

    result = await run_in_threadpool(run_regeneration_pipeline, req)

    logger.info(
        f"[MCP glasses_video_regenerate] Complete. {len(result.get('videos', []))} videos"
    )
    return result
