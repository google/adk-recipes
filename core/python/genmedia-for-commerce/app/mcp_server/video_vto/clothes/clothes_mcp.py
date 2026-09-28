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

"""MCP tool wrappers for the clothes video VTO pipeline."""

import base64
import logging

from workflows.video_vto.clothes.pipeline import (
    run_animate_model,
    run_video_vto,
)

logger = logging.getLogger(__name__)


async def run_video_vto_clothes(
    full_body_image_base64: str,
    garment_images_base64: list[str],
    scenario: str = "a plain white studio background",
    num_variations: int = 3,
    face_image_base64: str = "",
    number_of_videos: int = 4,
    prompt: str = "",
) -> dict:
    """Generate a video of a real person wearing garments — video virtual try-on (B2C).

    Consumer-facing tool: takes a real person's photo and one or more garment
    images, generates the best static VTO image, then animates it into a video.
    Unlike product fitting (B2B, single product on AI model), VTO supports
    multiple garments on a real person's photo with face preservation.

    Args:
        full_body_image_base64: Base64-encoded full body image of the person.
        garment_images_base64: List of base64-encoded garment images.
        scenario: Background description for the image generation step.
            Default: a plain white studio background.
        num_variations: Number of image VTO variations to try. Default: 3.
        face_image_base64: Optional base64-encoded face image for better preservation.
        number_of_videos: Number of videos to generate. Default: 4.
        prompt: Optional custom prompt for video generation.

    Returns:
        Dictionary with videos (base64), scores, image_base64 (best VTO image),
        and filenames.

    """
    if not full_body_image_base64:
        return {"error": "Full body image is required."}
    if not garment_images_base64:
        return {"error": "At least one garment image is required."}

    try:
        full_body_bytes = base64.b64decode(full_body_image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for full body image: {e}"}

    garment_bytes: list[bytes] = []
    for idx, img_b64 in enumerate(garment_images_base64):
        try:
            garment_bytes.append(base64.b64decode(img_b64))
        except Exception as e:
            return {"error": f"Invalid base64 for garment image {idx}: {e}"}

    face_bytes = None
    if face_image_base64:
        try:
            face_bytes = base64.b64decode(face_image_base64)
        except Exception as e:
            return {"error": f"Invalid base64 for face image: {e}"}

    logger.info(
        f"[MCP video_vto_clothes] Starting: {len(garment_bytes)} garments, "
        f"num_variations={num_variations}, number_of_videos={number_of_videos}"
    )

    result = {}
    async for event in run_video_vto(
        full_body_image=full_body_bytes,
        garment_images=garment_bytes,
        scenario=scenario,
        num_variations=num_variations,
        face_image=face_bytes,
        number_of_videos=number_of_videos,
        prompt=prompt,
    ):
        status = event.get("status")
        if status == "error":
            return {"error": event.get("detail", "Video VTO pipeline failed")}
        if status == "image_ready":
            result["image_base64"] = event.get("image_base64")
            result["image_final_score"] = event.get("final_score")
        if status == "videos":
            result["videos_base64"] = event.get("videos", [])
            result["scores"] = event.get("scores", [])
            result["filenames"] = event.get("filenames", [])

    if "videos_base64" not in result:
        return {"error": "Video generation did not produce results."}

    logger.info(
        f"[MCP video_vto_clothes] Complete. "
        f"{len(result.get('videos_base64', []))} videos generated"
    )
    return result


async def run_animate_model_mcp(
    model_image_base64: str,
    number_of_videos: int = 4,
    prompt: str = "",
) -> dict:
    """Animate a model image into catwalk-style videos (video-only, no image VTO).

    Takes an image of a model already wearing garments and generates
    animation videos using Veo R2V. Skips image VTO entirely.

    Args:
        model_image_base64: Base64-encoded image of the model wearing garments.
        number_of_videos: Number of videos to generate. Default: 4.
        prompt: Optional custom animation prompt. Defaults to catwalk sequence.

    Returns:
        Dictionary with videos_base64, scores, and filenames.

    """
    if not model_image_base64:
        return {"error": "Model image is required."}

    try:
        model_image_bytes = base64.b64decode(model_image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for model image: {e}"}

    logger.info(
        f"[MCP animate_model] Starting: number_of_videos={number_of_videos}"
    )

    result = {}
    async for event in run_animate_model(
        model_image=model_image_bytes,
        number_of_videos=number_of_videos,
        prompt=prompt,
    ):
        status = event.get("status")
        if status == "error":
            return {
                "error": event.get("detail", "Animate model pipeline failed")
            }
        if status == "videos":
            result["videos_base64"] = event.get("videos", [])
            result["scores"] = event.get("scores", [])
            result["filenames"] = event.get("filenames", [])

    if "videos_base64" not in result:
        return {"error": "Video generation did not produce results."}

    logger.info(
        f"[MCP animate_model] Complete. "
        f"{len(result.get('videos_base64', []))} videos generated"
    )
    return result
