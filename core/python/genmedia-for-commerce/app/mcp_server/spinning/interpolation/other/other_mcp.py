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

"""MCP tool wrapper for the interpolation spinning pipeline."""

import asyncio
import base64
import logging
import os

from google import genai
from workflows.shared.image_utils import preprocess_images
from workflows.shared.video_utils import merge_videos_from_bytes, reverse_video
from workflows.spinning.eval import (
    check_spin_direction,
    classify_product_type,
    glitch_detection,
)
from workflows.spinning.interpolation.other.interpolation_utils import (
    get_interpolation_prompt,
    process_single_video,
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


async def run_spinning_interpolation(
    images_base64: list[str],
    background_color: str = "#FFFFFF",
) -> dict:
    """Generate a 360-degree spinning video using frame interpolation between product images.

    Takes multiple product images from different angles and generates video segments
    between consecutive pairs, then merges them into a seamless spinning loop.
    Includes rotation direction and glitch validation for each segment.

    Args:
        images_base64: List of base64-encoded product images (at least 2).
            Images should show the product from sequential angles around it.
        background_color: Background color hex code. Default: #FFFFFF (white).

    Returns:
        Dictionary with video_base64 (merged spinning video as base64),
        num_segments, num_valid segments, and the prompt used.

    """
    if not images_base64 or len(images_base64) < 2:
        return {
            "error": "At least 2 product images are required for interpolation."
        }

    images_bytes = []
    for idx, img_b64 in enumerate(images_base64):
        try:
            images_bytes.append(base64.b64decode(img_b64))
        except Exception as e:
            return {"error": f"Invalid base64 encoding for image {idx}: {e}"}

    client, veo_client = _get_clients()

    logger.info(
        f"[MCP spinning_interpolation] Starting with {len(images_bytes)} images"
    )

    loop = asyncio.get_event_loop()

    # Step 1: Preprocess images
    processed_images = await loop.run_in_executor(
        None,
        lambda: preprocess_images(
            images_bytes_list=images_bytes,
            client=client,
            upscale_client=veo_client,
            num_workers=4,
            upscale_images=True,
            create_canva=True,
        ),
    )

    # Step 2: Generate prompt from first two images
    prompt_text = await loop.run_in_executor(
        None,
        lambda: get_interpolation_prompt(
            client=client,
            gemini_model=os.getenv("MODEL_NAME_GENERATED_2"),
            all_images_bytes=[processed_images[0], processed_images[1]],
        ),
    )

    # Classify product type from first image (glasses need different rotation eval)
    product_type = await loop.run_in_executor(
        None, classify_product_type, veo_client, images_bytes[0]
    )
    logger.info(f"[MCP spinning_interpolation] Product type: {product_type}")

    # Step 3: Generate all video segments in parallel
    num_videos = len(processed_images)

    async def generate_segment(index: int) -> bytes:
        start_idx = index
        end_idx = (index + 1) % len(processed_images)
        return await loop.run_in_executor(
            None,
            lambda si=start_idx, ei=end_idx, idx=index: process_single_video(
                client=veo_client,
                start_image=processed_images[si],
                end_image=processed_images[ei],
                prompt=prompt_text,
                index=idx,
                num_frames_for_similarity=15,
                background_color=background_color,
            ),
        )

    tasks = [generate_segment(i) for i in range(num_videos)]
    videos = list(await asyncio.gather(*tasks))

    # Step 4: Validate each video (rotation + glitch check) in parallel
    valid_count = 0

    async def check_rotation(i):
        direction = await loop.run_in_executor(
            None, check_spin_direction, videos[i], product_type, client
        )
        return i, direction

    for attempt in range(MAX_CONSISTENCY_RETRIES + 1):
        pending = list(range(len(videos)))
        rotation_tasks = [check_rotation(i) for i in pending]
        rotation_results = await asyncio.gather(*rotation_tasks)

        needs_regen = []
        for i, direction in rotation_results:
            if direction == "anticlockwise":
                videos[i] = await loop.run_in_executor(
                    None, reverse_video, videos[i]
                )
            elif direction == "invalid":
                needs_regen.append(i)

        # Glitch detection for all non-regenerated
        glitch_tasks = [
            loop.run_in_executor(None, glitch_detection, veo_client, videos[i])
            for i in range(len(videos))
            if i not in needs_regen
        ]
        glitch_results = await asyncio.gather(*glitch_tasks)
        glitch_idx = [i for i in range(len(videos)) if i not in needs_regen]

        for i, glitch_result in zip(glitch_idx, glitch_results, strict=False):
            if glitch_result["is_valid"]:
                valid_count += 1
            else:
                needs_regen.append(i)

        if not needs_regen or attempt >= MAX_CONSISTENCY_RETRIES:
            break

        # Regenerate failed segments
        regen_tasks = [generate_segment(i) for i in needs_regen]
        regen_results = await asyncio.gather(*regen_tasks)
        for i, new_video in zip(needs_regen, regen_results, strict=False):
            videos[i] = new_video

    # Step 5: Merge all segments
    speeds = [1.0] * len(videos)
    merged_video = await loop.run_in_executor(
        None,
        lambda: merge_videos_from_bytes(videos_bytes=videos, speeds=speeds),
    )

    video_base64 = base64.b64encode(merged_video).decode("utf-8")

    logger.info(
        f"[MCP spinning_interpolation] Complete. {num_videos} segments, "
        f"{valid_count}/{num_videos} valid"
    )
    return {
        "video_base64": video_base64,
        "num_segments": num_videos,
        "num_valid": valid_count,
        "prompt": prompt_text,
    }
