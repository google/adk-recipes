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

"""REST API endpoints for the interpolation spinning workflow."""

import asyncio
import base64
import io
import json
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from google import genai
from workflows.shared.image_utils import preprocess_images
from workflows.shared.video_utils import merge_videos_from_bytes
from workflows.spinning.eval import (
    check_spin_direction,
    glitch_detection,
)
from workflows.spinning.interpolation.other.interpolation_utils import (
    get_interpolation_prompt,
    process_single_video,
)

logger = logging.getLogger(__name__)

MAX_CONSISTENCY_RETRIES = 3

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

client = genai.Client(vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION)
veo_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)

router = APIRouter(
    prefix="/api/spinning/interpolation/other",
    tags=["Spinning Interpolation"],
)

# Gallery images directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
_interpolation_dir = os.path.normpath(
    os.path.join(
        _script_dir,
        os.pardir,
        os.pardir,
        os.pardir,
        os.pardir,
        "workflows",
        "spinning",
        "interpolation",
        "other",
    )
)
_products_dir = os.path.join(_interpolation_dir, "images")


@router.get("/get_gallery_images")
async def get_gallery_images():
    """Return available gallery images grouped by product folder."""
    products = []

    if not os.path.exists(_products_dir):
        return JSONResponse(content={"products": []})

    try:
        for folder_name in sorted(os.listdir(_products_dir)):
            folder_path = os.path.join(_products_dir, folder_name)
            if os.path.isdir(folder_path) and folder_name.startswith(
                "product_"
            ):
                images = []
                for filename in sorted(os.listdir(folder_path)):
                    if filename.lower().endswith((".png", ".jpg", ".jpeg")):
                        image_url = f"/other/images/products_interpolation/{folder_name}/{filename}"
                        images.append({"url": image_url, "name": filename})
                if images:
                    products.append(
                        {"folder_name": folder_name, "images": images}
                    )

        return JSONResponse(content={"products": products})
    except Exception as e:
        logger.error(f"Error scanning gallery images: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/preprocess")
async def interpolation_preprocess(images: list[UploadFile] = File(...)):  # noqa: B008
    """Preprocess images for interpolation video generation."""
    if not images:
        raise HTTPException(status_code=400, detail="No images provided.")

    try:
        images_bytes_list = [await image.read() for image in images]

        processed_images_bytes = await run_in_threadpool(
            preprocess_images,
            images_bytes_list=images_bytes_list,
            client=client,
            upscale_client=veo_client,
            num_workers=4,
            upscale_images=True,
            create_canva=True,
        )

        processed_images = []
        for idx, img_bytes in enumerate(processed_images_bytes):
            processed_images.append(
                {
                    "index": idx,
                    "data": base64.b64encode(img_bytes).decode("utf-8"),
                }
            )

        return JSONResponse(content={"images": processed_images})
    except Exception as e:
        logger.error(f"[Interpolation Preprocess] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/generate-prompt")
async def interpolation_generate_prompt(
    img1: UploadFile = File(...),  # noqa: B008
    img2: UploadFile = File(...),  # noqa: B008
):
    """Generate a video prompt for interpolation between two frames."""
    img1_bytes = await img1.read()
    img2_bytes = await img2.read()

    prompt_text = await run_in_threadpool(
        get_interpolation_prompt,
        client=client,
        gemini_model=os.getenv("MODEL_NAME_GENERATED_2"),
        all_images_bytes=[img1_bytes, img2_bytes],
    )

    return JSONResponse(content={"prompt": prompt_text})


@router.post("/generate-all")
async def interpolation_generate_all(
    images: list[UploadFile] = File(...),  # noqa: B008
    prompt: str = Form(""),
    backgroundColor: str = Form("#FFFFFF"),
    indices: str = Form(""),
):
    """Generate video segments for interpolation with rotation checking."""
    if len(images) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least 2 images required for interpolation.",
        )

    try:
        images_bytes = [await img.read() for img in images]
        num_videos = len(images_bytes)

        # Auto-generate prompt if not provided
        if not prompt.strip():
            logger.info(
                "[Interpolation] No prompt provided, generating from images..."
            )
            prompt = await run_in_threadpool(
                get_interpolation_prompt,
                client=client,
                gemini_model=os.getenv("MODEL_NAME_GENERATED_2"),
                all_images_bytes=images_bytes,
            )
            logger.info(f"[Interpolation] Generated prompt: {prompt[:100]}...")

        if indices.strip() == "" or indices.strip() == "[]":
            indices_to_generate = list(range(num_videos))
        else:
            indices_to_generate = json.loads(indices)
            for idx in indices_to_generate:
                if idx < 0 or idx >= num_videos:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid index {idx}, must be between 0 and {num_videos - 1}",
                    )

        async def generate_video_segment(index: int) -> bytes:
            start_idx = index
            end_idx = (index + 1) % len(images_bytes)
            return await run_in_threadpool(
                process_single_video,
                client=veo_client,
                start_image=images_bytes[start_idx],
                end_image=images_bytes[end_idx],
                prompt=prompt,
                index=index,
                num_frames_for_similarity=15,
                background_color=backgroundColor,
            )

        tasks = [generate_video_segment(i) for i in indices_to_generate]
        videos = list(await asyncio.gather(*tasks))

        retry_counts = [0] * len(indices_to_generate)
        validation_status = [
            {"is_valid": False, "reason": ""} for _ in indices_to_generate
        ]

        async def check_and_regenerate():
            regenerated = False
            for i, video_index in enumerate(indices_to_generate):
                if validation_status[i]["is_valid"]:
                    continue

                direction = await run_in_threadpool(
                    check_spin_direction, video_bytes=videos[i]
                )

                if direction != "clockwise":
                    if retry_counts[i] < MAX_CONSISTENCY_RETRIES:
                        retry_counts[i] += 1
                        videos[i] = await generate_video_segment(video_index)
                        regenerated = True
                    else:
                        validation_status[i] = {
                            "is_valid": False,
                            "reason": f"{direction} rotation after max retries",
                        }
                    continue

                glitch_result = await run_in_threadpool(
                    glitch_detection, client=veo_client, video_bytes=videos[i]
                )

                if not glitch_result["is_valid"]:
                    if retry_counts[i] < MAX_CONSISTENCY_RETRIES:
                        retry_counts[i] += 1
                        videos[i] = await generate_video_segment(video_index)
                        regenerated = True
                    else:
                        validation_status[i] = {
                            "is_valid": False,
                            "reason": glitch_result["explanation"],
                        }
                else:
                    validation_status[i] = {
                        "is_valid": True,
                        "reason": "Passed rotation and glitch checks",
                    }

            return regenerated

        while True:
            needs_regeneration = await check_and_regenerate()
            if not needs_regeneration:
                break

        videos_response = []
        num_valid = 0
        num_failed = 0

        for i, video_bytes in enumerate(videos):
            video_data = {
                "index": indices_to_generate[i],
                "data": base64.b64encode(video_bytes).decode("utf-8"),
                "retries": retry_counts[i],
                "is_valid": validation_status[i]["is_valid"],
                "validation_reason": validation_status[i]["reason"],
            }
            videos_response.append(video_data)

            if validation_status[i]["is_valid"]:
                num_valid += 1
            else:
                num_failed += 1

        return JSONResponse(
            content={
                "videos": videos_response,
                "num_videos": num_videos,
                "num_generated": len(videos),
                "num_valid": num_valid,
                "num_failed": num_failed,
                "indices": indices_to_generate,
                "total_retries": sum(retry_counts),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Interpolation Generate All] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/merge")
async def interpolation_merge(
    videos: list[UploadFile] = File(...),  # noqa: B008
    speeds: str = Form(...),
):
    """Merge multiple video segments into one final video."""
    if not videos:
        raise HTTPException(status_code=400, detail="No video files provided.")

    try:
        video_bytes_list = [await video.read() for video in videos]
        speed_list = json.loads(speeds)

        merged_video_bytes = await run_in_threadpool(
            merge_videos_from_bytes,
            videos_bytes=video_bytes_list,
            speeds=speed_list,
        )

        return StreamingResponse(
            io.BytesIO(merged_video_bytes),
            media_type="video/mp4",
            headers={
                "Content-Disposition": "attachment; filename=merged_video.mp4"
            },
        )
    except Exception as e:
        logger.error(f"[Interpolation Merge] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
