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

"""REST API endpoints for the other products spinning R2V workflow."""

import base64
import io
import json
import logging
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from google import genai
from jinja2 import Template
from workflows.shared.image_utils import (
    preprocess_images,
    stack_and_canvas_images,
)
from workflows.shared.video_utils import merge_videos_from_bytes
from workflows.spinning.eval import check_spin_direction, glitch_detection
from workflows.spinning.r2v.other.pipeline import generate_video_r2v
from workflows.spinning.r2v.other.r2v_utils import (
    VEO_R2V_PROMPT_TEMPLATE,
    generate_product_description,
)

logger = logging.getLogger(__name__)

MAX_CONSISTENCY_RETRIES = 3

PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

client = genai.Client(vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION)
veo_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)
gemini_model = os.getenv("MODEL_NAME_GENERATED_1")

router = APIRouter(
    prefix="/api/spinning/r2v/other",
    tags=["Spinning R2V Other"],
)

# Gallery images directory
_script_dir = os.path.dirname(os.path.abspath(__file__))
_r2v_other_dir = os.path.normpath(
    os.path.join(
        _script_dir,
        os.pardir,
        os.pardir,
        os.pardir,
        os.pardir,
        "workflows",
        "spinning",
        "r2v",
        "other",
    )
)
_products_r2v_dir = os.path.join(_r2v_other_dir, "images")


@router.get("/get_gallery_images")
async def get_gallery_images():
    """Return available gallery images grouped by product folder."""
    products = []

    if not os.path.exists(_products_r2v_dir):
        return JSONResponse(content={"products": []})

    try:
        for folder_name in sorted(os.listdir(_products_r2v_dir)):
            folder_path = os.path.join(_products_r2v_dir, folder_name)
            if os.path.isdir(folder_path) and folder_name.startswith(
                "product_"
            ):
                images = []
                for filename in sorted(os.listdir(folder_path)):
                    if filename.lower().endswith((".png", ".jpg", ".jpeg")):
                        image_url = f"/other/images/products_r2v/{folder_name}/{filename}"
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
async def r2v_preprocess(images: list[UploadFile] = File(...)):  # noqa: B008
    """Preprocess product images for R2V generation."""
    if not images:
        raise HTTPException(status_code=400, detail="No images provided.")
    if len(images) > 4:
        raise HTTPException(status_code=400, detail="Maximum 4 images allowed.")

    try:
        images_bytes_list = [await image.read() for image in images]

        processed_images_bytes = await run_in_threadpool(
            preprocess_images,
            images_bytes_list=images_bytes_list,
            client=client,
            upscale_client=veo_client,
            num_workers=4,
            upscale_images=True,
            create_canva=False,
        )

        reference_images = await run_in_threadpool(
            stack_and_canvas_images, processed_images_bytes
        )

        results = []
        for idx, img_bytes in enumerate(reference_images):
            results.append(
                {
                    "index": idx,
                    "image_base64": base64.b64encode(img_bytes).decode("utf-8"),
                }
            )

        return JSONResponse(
            content={"processed_images": results, "num_processed": len(results)}
        )
    except Exception as e:
        logger.error(f"[R2V Preprocess] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/generate-prompt")
async def r2v_generate_prompt(images: list[UploadFile] = File(...)):  # noqa: B008
    """Generate a prompt for product spinning video based on images."""
    images_bytes = [await img.read() for img in images]

    try:
        product_description = await run_in_threadpool(
            generate_product_description,
            client=client,
            gemini_model=gemini_model,
            all_images_bytes=images_bytes,
        )

        prompt_text = Template(VEO_R2V_PROMPT_TEMPLATE).render(
            {"description": product_description}
        )

        return JSONResponse(
            content={"prompt": prompt_text, "description": product_description}
        )
    except Exception as e:
        logger.error(f"[R2V Generate Prompt] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/generate")
async def r2v_generate(
    reference_images: list[UploadFile] = File(...),  # noqa: B008
    prompt: str = Form(...),
    index: int = Form(0),
):
    """Generate a single 360 spinning video using reference images."""
    try:
        reference_images_bytes = [await img.read() for img in reference_images]

        video_bytes = None
        retries = 0
        is_valid = False
        validation_reason = ""

        for attempt in range(MAX_CONSISTENCY_RETRIES + 1):
            video_bytes = await run_in_threadpool(
                generate_video_r2v, reference_images_bytes, prompt, index
            )

            is_clockwise = await run_in_threadpool(
                check_spin_direction, video_bytes
            )

            if not is_clockwise:
                retries += 1
                validation_reason = "Not rotating clockwise"
                if attempt < MAX_CONSISTENCY_RETRIES:
                    continue
                break

            glitch_result = await run_in_threadpool(
                glitch_detection, client=veo_client, video_bytes=video_bytes
            )

            if glitch_result["is_valid"]:
                is_valid = True
                validation_reason = "Passed all checks"
                break
            else:
                retries += 1
                validation_reason = glitch_result["explanation"]
                if attempt >= MAX_CONSISTENCY_RETRIES:
                    break

        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "X-Video-Filename": f"vid_r2v_{index:04d}.mp4",
                "X-Retries": str(retries),
                "X-Is-Valid": str(is_valid),
                "X-Validation-Reason": validation_reason,
            },
        )
    except Exception as e:
        logger.error(
            f"[R2V Generate] Error for index {index}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/pipeline")
async def r2v_pipeline(images: list[UploadFile] = File(...)):  # noqa: B008
    """Full end-to-end R2V pipeline: preprocess + generate prompt + generate video."""
    if not images:
        raise HTTPException(status_code=400, detail="No images provided.")
    if len(images) > 4:
        raise HTTPException(status_code=400, detail="Maximum 4 images allowed.")

    try:
        images_bytes_list = [await image.read() for image in images]

        processed_images_bytes = await run_in_threadpool(
            preprocess_images,
            images_bytes_list=images_bytes_list,
            client=client,
            upscale_client=veo_client,
            num_workers=4,
            upscale_images=True,
            create_canva=False,
        )

        reference_images = await run_in_threadpool(
            stack_and_canvas_images, processed_images_bytes
        )

        product_description = await run_in_threadpool(
            generate_product_description,
            client=client,
            gemini_model=gemini_model,
            all_images_bytes=images_bytes_list,
        )
        prompt_text = Template(VEO_R2V_PROMPT_TEMPLATE).render(
            {"description": product_description}
        )

        video_bytes = None
        retries = 0
        is_valid = False
        validation_reason = ""

        for attempt in range(MAX_CONSISTENCY_RETRIES + 1):
            video_bytes = await run_in_threadpool(
                generate_video_r2v, reference_images, prompt_text, 0
            )

            is_clockwise = await run_in_threadpool(
                check_spin_direction, video_bytes
            )

            if not is_clockwise:
                retries += 1
                validation_reason = "Not rotating clockwise"
                if attempt < MAX_CONSISTENCY_RETRIES:
                    continue
                break

            glitch_result = await run_in_threadpool(
                glitch_detection, client=veo_client, video_bytes=video_bytes
            )

            if glitch_result["is_valid"]:
                is_valid = True
                validation_reason = "Passed all checks"
                break
            else:
                retries += 1
                validation_reason = glitch_result["explanation"]
                if attempt >= MAX_CONSISTENCY_RETRIES:
                    break

        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "X-Video-Filename": "r2v_pipeline.mp4",
                "X-Retries": str(retries),
                "X-Is-Valid": str(is_valid),
                "X-Validation-Reason": validation_reason,
            },
        )
    except Exception as e:
        logger.error(f"[R2V Pipeline] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/merge")
async def r2v_merge(
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
        logger.error(f"[R2V Merge] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
