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

"""MCP tool wrappers for the glasses image VTO pipeline."""

import asyncio
import base64
import logging
import os

from fastapi.concurrency import run_in_threadpool
from google import genai
from workflows.image_vto.glasses.image_generation import (
    edit_frame_nano,
    enhance_photo_nano,
)
from workflows.image_vto.glasses.pipeline import run_glasses_vto
from workflows.shared.image_utils import replace_background

logger = logging.getLogger(__name__)


def _get_clients():
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    genai_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    nano_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return genai_client, nano_client


async def run_glasses_vto_tool(
    model_image_base64: str,
    product_images_base64: list[str],
    num_variations: int = 3,
) -> dict:
    """Generate glasses virtual try-on images showing a person wearing glasses.

    Takes a face photo and glasses product images, then generates realistic
    images of the person wearing those glasses.

    Args:
        model_image_base64: Base64-encoded front face image of the person.
        product_images_base64: List of base64-encoded glasses product images (1-2).
        num_variations: Number of variations to generate. Default: 3.

    Returns:
        Dictionary with best result including image_base64, final_score,
        face_score, and glasses_evaluation.

    """
    if not model_image_base64:
        return {"error": "Model face image is required."}
    if not product_images_base64:
        return {"error": "At least one glasses image is required."}

    try:
        model_bytes = base64.b64decode(model_image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for model image: {e}"}

    glasses_bytes: list[bytes] = []
    for idx, img_b64 in enumerate(product_images_base64):
        try:
            glasses_bytes.append(base64.b64decode(img_b64))
        except Exception as e:
            return {"error": f"Invalid base64 for glasses image {idx}: {e}"}

    logger.info(
        f"[MCP glasses_vto] Starting: {len(glasses_bytes)} glasses images, "
        f"num_variations={num_variations}"
    )

    best_result = None
    best_score = -1.0

    async for result in run_glasses_vto(
        model_bytes, glasses_bytes, num_variations
    ):
        if result.status == "failed" and result.index == -1:
            return {"error": result.error or "Pipeline failed"}
        if result.status == "complete":
            break
        if result.status == "ready" and result.image is not None:
            score = result.final_score or 0.0
            if score > best_score:
                best_score = score
                best_result = result

    if best_result is None:
        return {"error": "No valid glasses VTO result was generated."}

    response = {
        "image_base64": best_result.image_base64
        or base64.b64encode(best_result.image).decode("utf-8"),
        "final_score": best_result.final_score,
        "face_score": best_result.face_score,
    }
    if best_result.evaluation:
        response["evaluation"] = best_result.evaluation
    if best_result.glasses_evaluation:
        response["glasses_evaluation"] = best_result.glasses_evaluation

    logger.info(f"[MCP glasses_vto] Complete. score={best_score:.1f}")
    return response


async def run_glasses_enhance(
    image_base64: str,
    view_type: str = "front",
) -> dict:
    """Generate 4 enhanced variations of a glasses product image.

    Args:
        image_base64: Base64-encoded glasses image to enhance.
        view_type: View type for enhancement. Default: front.

    Returns:
        Dictionary with enhanced_images list of base64-encoded images.

    """
    try:
        image_bytes = base64.b64decode(image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for image: {e}"}

    genai_client, nano_client = _get_clients()

    cropped = await run_in_threadpool(
        replace_background, genai_client, image_bytes, 0.01, None
    )

    tasks = [
        run_in_threadpool(enhance_photo_nano, nano_client, cropped, view_type)
        for _ in range(4)
    ]
    enhanced_images = await asyncio.gather(*tasks)

    return {
        "enhanced_images": [
            base64.b64encode(img).decode("utf-8") for img in enhanced_images
        ],
    }


async def run_glasses_edit_frame(
    prompt: str,
    generated_image_base64: str,
) -> dict:
    """Edit an existing generated glasses frame image based on a text prompt.

    Args:
        prompt: Text description of the desired edit.
        generated_image_base64: Base64-encoded image to edit.

    Returns:
        Dictionary with edited_frame_image as base64-encoded image.

    """
    try:
        image_bytes = base64.b64decode(generated_image_base64)
    except Exception as e:
        return {"error": f"Invalid base64 for image: {e}"}

    _, nano_client = _get_clients()

    edited = await run_in_threadpool(
        edit_frame_nano, nano_client, prompt, image_bytes
    )

    if not edited:
        return {"error": "Frame editing failed - no image returned"}

    return {
        "edited_frame_image": base64.b64encode(edited).decode("utf-8"),
    }
