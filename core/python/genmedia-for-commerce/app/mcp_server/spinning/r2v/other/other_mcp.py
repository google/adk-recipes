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

"""MCP tool wrapper for the other products spinning R2V pipeline."""

import asyncio
import base64
import logging
import os

from google import genai
from jinja2 import Template
from workflows.shared.image_utils import (
    preprocess_images,
    stack_and_canvas_images,
)
from workflows.shared.video_utils import reverse_video
from workflows.spinning.eval import check_spin_direction, glitch_detection
from workflows.spinning.r2v.other.image_selection import select_best_images
from workflows.spinning.r2v.other.pipeline import generate_video_r2v
from workflows.spinning.r2v.other.r2v_utils import (
    VEO_R2V_PROMPT_TEMPLATE,
    generate_product_description,
)

logger = logging.getLogger(__name__)

MAX_CONSISTENCY_RETRIES = 5


def _get_clients():
    project_id = os.getenv("PROJECT_ID", "my_project")
    global_region = os.getenv("GLOBAL_REGION", "global")
    client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    veo_client = genai.Client(
        vertexai=True, project=project_id, location=global_region
    )
    return client, veo_client


async def run_spinning_other_r2v(
    images_base64: list[str],
) -> dict:
    """Generate a 360-degree spinning video of any product using reference-to-video (R2V).

    Works with any product type (not just shoes). Takes product images from
    multiple angles and generates a smooth spinning video. Includes rotation
    direction and glitch validation.

    Args:
        images_base64: List of base64-encoded product images (1-4 images).
            Include images from multiple angles for best results.

    Returns:
        Dictionary with video_base64 (base64-encoded MP4 video),
        description, prompt, retries count, and validation status.

    """
    if not images_base64:
        return {"error": "At least one product image is required."}

    images_bytes = []
    for idx, img_b64 in enumerate(images_base64):
        try:
            images_bytes.append(base64.b64decode(img_b64))
        except Exception as e:
            return {"error": f"Invalid base64 encoding for image {idx}: {e}"}

    client, veo_client = _get_clients()
    gemini_model = os.getenv("MODEL_NAME_GENERATED_1")

    logger.info(
        f"[MCP spinning_other_r2v] Starting with {len(images_bytes)} images"
    )

    loop = asyncio.get_event_loop()

    # Step 0: Select best images if more than 4
    if len(images_bytes) > 4:
        images_bytes = await loop.run_in_executor(
            None, select_best_images, client, images_bytes, gemini_model
        )
        logger.info(
            f"[MCP spinning_other_r2v] Selected {len(images_bytes)} images after classification"
        )

    # Step 1: Preprocess images
    processed_images_bytes = await loop.run_in_executor(
        None,
        lambda: preprocess_images(
            images_bytes_list=images_bytes,
            client=client,
            upscale_client=veo_client,
            num_workers=4,
            upscale_images=True,
            create_canva=False,
        ),
    )

    reference_images = await loop.run_in_executor(
        None, stack_and_canvas_images, processed_images_bytes
    )

    # Step 2: Generate prompt
    product_description = await loop.run_in_executor(
        None,
        lambda: generate_product_description(
            client=client,
            gemini_model=gemini_model,
            all_images_bytes=images_bytes,
        ),
    )
    prompt_text = Template(VEO_R2V_PROMPT_TEMPLATE).render(
        {"description": product_description}
    )

    # Step 3: Generate video with validation retries
    video_bytes = None
    retries = 0
    is_valid = False

    for attempt in range(MAX_CONSISTENCY_RETRIES + 1):
        video_bytes = await loop.run_in_executor(
            None, generate_video_r2v, reference_images, prompt_text, 0
        )

        direction = await loop.run_in_executor(
            None, check_spin_direction, video_bytes
        )

        if direction == "anticlockwise":
            logger.info("[MCP spinning_other_r2v] Anticlockwise, reversing")
            video_bytes = await loop.run_in_executor(
                None, reverse_video, video_bytes
            )
        elif direction == "invalid":
            retries += 1
            if attempt < MAX_CONSISTENCY_RETRIES:
                continue
            break

        glitch_result = await loop.run_in_executor(
            None, glitch_detection, veo_client, video_bytes
        )

        if glitch_result["is_valid"]:
            is_valid = True
            break
        else:
            retries += 1
            if attempt >= MAX_CONSISTENCY_RETRIES:
                break

    video_base64 = base64.b64encode(video_bytes).decode("utf-8")

    logger.info(
        f"[MCP spinning_other_r2v] Complete. retries={retries}, valid={is_valid}"
    )
    return {
        "video_base64": video_base64,
        "retries": retries,
        "is_valid": is_valid,
        "description": product_description,
        "prompt": prompt_text,
    }
