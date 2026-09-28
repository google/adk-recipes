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

"""Glasses Video VTO pipeline functions."""

import base64
import logging
import os
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial

from google import genai
from pydantic import BaseModel
from workflows.shared.image_utils import replace_background
from workflows.video_vto.glasses.generate_video_util import (
    create_collage,
    generate_veo,
    process_veo_video_model_on_fit,
)
from workflows.video_vto.glasses.glasses_eval import check_video_for_glitches

logger = logging.getLogger(__name__)

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

genai_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)
veo_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)


def post_process_video(video_bytes, bgcolor=None):
    """Post-process a VEO video: trim green screen and check for glitches.

    Returns None if processing fails or glitches are detected.
    """
    if bgcolor is None:
        bgcolor = [0, 215, 6, 255]
    try:
        processed_video_bytes = process_veo_video_model_on_fit(
            video_bytes=video_bytes, bgcolor=bgcolor
        )
        if not processed_video_bytes:
            logger.warning(
                "process_veo_video_model_on_fit returned empty bytes, video will be skipped"
            )
            return None

        glitch_result = check_video_for_glitches(
            genai_client, processed_video_bytes
        )

        if glitch_result and glitch_result.get("is_glitched", False):
            logger.warning(
                f"Video discarded due to glitches: {glitch_result.get('reason', 'Unknown reason')}"
            )
            return None
        else:
            logger.info(
                f"Video passed glitch detection: {glitch_result.get('reason', 'No reason provided')}"
            )

        return processed_video_bytes
    except Exception as e:
        logger.error(f"Error in post_process_video: {e}")
        return None


class RegenerationRequest(BaseModel):
    """Request schema for video regeneration."""

    prompt: str
    collage_data: str
    number_of_videos: int = 1
    bg_color: str = "0,215,6,255"
    is_animation_mode: bool = False


def run_regeneration_pipeline(req: RegenerationRequest):
    """Run the video regeneration pipeline."""
    collage_bytes = base64.b64decode(req.collage_data)

    try:
        bg_color = tuple(map(int, req.bg_color.split(",")))
    except ValueError:
        logger.warning(
            f"Invalid background color format: {req.bg_color}, using default"
        )
        bg_color = (0, 215, 6, 255)

    video_bytes_list = generate_veo(
        veo_client,
        collage_img=collage_bytes,
        veo_prompt=req.prompt,
        total_videos=req.number_of_videos,
        duration_seconds=8,
    )

    if req.is_animation_mode:
        processed_video_bytes_list = video_bytes_list
    else:
        with ProcessPoolExecutor() as executor:
            post_process_with_bgcolor = partial(
                post_process_video, bgcolor=bg_color
            )
            all_processed_results = list(
                executor.map(post_process_with_bgcolor, video_bytes_list)
            )
            processed_video_bytes_list = [
                video for video in all_processed_results if video is not None
            ]

            if len(processed_video_bytes_list) < len(video_bytes_list):
                logger.warning(
                    f"Some videos failed post-processing during regeneration: "
                    f"{len(processed_video_bytes_list)}/{len(video_bytes_list)} videos successful"
                )

    if not processed_video_bytes_list:
        raise Exception(
            "All videos failed processing during regeneration - no valid videos to return."
        )

    encoded_videos = [
        base64.b64encode(video_bytes).decode("utf-8")
        for video_bytes in processed_video_bytes_list
    ]
    video_filenames = [
        f"collage_video_{uuid.uuid4()}.mp4" for _ in processed_video_bytes_list
    ]

    logger.info(f"Successfully regenerated {len(encoded_videos)} videos.")
    return {"videos": encoded_videos, "filenames": video_filenames}


def run_generation_pipeline(
    prompt,
    number_of_videos,
    model_image_bytes,
    product_image_bytes,
    model_side_image_bytes=None,
    is_template_product_image=False,
    bg_color=(0, 215, 6, 255),
    zoom_level=0,
    is_animation_mode=False,
):
    """Run the initial video generation pipeline."""
    if is_animation_mode and model_image_bytes:
        logger.info("Animation mode: using model image directly")
        collage_bytes = model_image_bytes
    else:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}

            if model_image_bytes:
                futures["model"] = executor.submit(
                    replace_background,
                    genai_client,
                    model_image_bytes,
                    0.01,
                    None,
                )
            if model_side_image_bytes:
                futures["model_side"] = executor.submit(
                    replace_background,
                    genai_client,
                    model_side_image_bytes,
                    0.01,
                    None,
                )
            if product_image_bytes:
                futures["product"] = executor.submit(
                    replace_background,
                    genai_client,
                    product_image_bytes,
                    0.01,
                    None,
                )

            if "model" in futures:
                model_image_bytes = futures["model"].result()
            if "model_side" in futures:
                model_side_image_bytes = futures["model_side"].result()
            if "product" in futures:
                product_image_bytes = futures["product"].result()

        margin = (6 - zoom_level) * 100

        collage_bytes = create_collage(
            model_image_bytes=model_image_bytes,
            glasses_image_bytes=product_image_bytes,
            model_side_image_bytes=model_side_image_bytes,
            horizontal_margin=margin,
            vertical_margin=margin / 2,
            image_spacing=300,
            bg_color=bg_color,
        )

        if not collage_bytes:
            raise Exception("create_collage returned empty bytes.")

    encoded_collage = base64.b64encode(collage_bytes).decode("utf-8")

    video_bytes_list = generate_veo(
        veo_client,
        collage_img=collage_bytes,
        veo_prompt=prompt,
        total_videos=number_of_videos,
        duration_seconds=8,
    )

    if is_animation_mode:
        processed_video_bytes_list = video_bytes_list
    else:
        with ProcessPoolExecutor() as executor:
            post_process_with_bgcolor = partial(
                post_process_video, bgcolor=bg_color
            )
            all_processed_results = list(
                executor.map(post_process_with_bgcolor, video_bytes_list)
            )
            processed_video_bytes_list = [
                video for video in all_processed_results if video is not None
            ]

            if len(processed_video_bytes_list) < len(video_bytes_list):
                logger.warning(
                    f"Some videos failed post-processing: "
                    f"{len(processed_video_bytes_list)}/{len(video_bytes_list)} videos successful"
                )

    if not processed_video_bytes_list:
        logger.warning(
            "All videos failed processing - returning empty response."
        )
        return {
            "videos": [],
            "filenames": [],
            "collage_data": encoded_collage,
        }

    encoded_videos = [
        base64.b64encode(video_bytes).decode("utf-8")
        for video_bytes in processed_video_bytes_list
    ]
    video_filenames = [
        f"{'animation' if is_animation_mode else 'collage'}_video_{uuid.uuid4()}.mp4"
        for _ in processed_video_bytes_list
    ]

    logger.info(f"Successfully processed {len(encoded_videos)} videos.")
    return {
        "videos": encoded_videos,
        "filenames": video_filenames,
        "collage_data": encoded_collage,
    }
