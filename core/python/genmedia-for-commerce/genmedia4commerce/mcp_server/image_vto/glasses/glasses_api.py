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

"""REST API endpoints for the glasses image VTO workflow."""

import asyncio
import base64
import json
import logging
import os
from functools import partial

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from google import genai
from starlette.responses import StreamingResponse
from workflows.image_vto.glasses.image_generation import (
    edit_frame_nano,
    enhance_photo_nano,
)
from workflows.image_vto.glasses.pipeline import run_glasses_vto
from workflows.shared.image_utils import replace_background

logger = logging.getLogger(__name__)

# Gemini clients
PROJECT_ID = os.getenv("PROJECT_ID", "my_project")
GLOBAL_REGION = os.getenv("GLOBAL_REGION", "global")

genai_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)
nano_client = genai.Client(
    vertexai=True, project=PROJECT_ID, location=GLOBAL_REGION
)

router = APIRouter(
    prefix="/api/glasses",
    tags=["Glasses Image VTO"],
)

# Images directory for gallery
_script_dir = os.path.dirname(os.path.abspath(__file__))
_glasses_workflow_dir = os.path.join(
    os.path.dirname(_script_dir),  # image_vto/
    os.pardir,
    os.pardir,  # mcp_server/
    "workflows",
    "image_vto",
    "glasses",
)
_images_dir = os.path.normpath(os.path.join(_glasses_workflow_dir, "images"))


@router.get("/get_gallery_images")
async def get_gallery_images():
    """Return a list of available gallery images."""
    image_files = []
    if os.path.exists(_images_dir):
        for filename in os.listdir(_images_dir):
            if filename.lower().endswith((".png", ".jpg", ".jpeg")):
                image_files.append(
                    {"url": f"/glasses/images/{filename}", "name": filename}
                )
    return JSONResponse(content={"images": image_files})


@router.post("/enhance-image")
async def enhance_image_endpoint(
    image: UploadFile = File(...),  # noqa: B008
    view_type: str = Form("front"),
):
    """Generate 4 enhanced variations of the uploaded image using AI."""
    try:
        image_bytes = await image.read()

        cropped_image_bytes = await run_in_threadpool(
            replace_background, genai_client, image_bytes, 0.01, None
        )

        enhance_func = partial(
            enhance_photo_nano, nano_client, cropped_image_bytes, view_type
        )

        tasks = [run_in_threadpool(enhance_func) for _ in range(4)]
        enhanced_images = await asyncio.gather(*tasks)

        enhanced_images_b64 = [
            base64.b64encode(img_bytes).decode("utf-8")
            for img_bytes in enhanced_images
        ]

        return JSONResponse(
            content={
                "enhanced_images": enhanced_images_b64,
                "original_filename": image.filename,
            }
        )
    except Exception as e:
        logger.error(f"Error in /enhance-image: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e


@router.post("/generate-vto")
async def generate_vto_endpoint(
    model_image: UploadFile = File(...),  # noqa: B008
    product_image: UploadFile | None = File(None),  # noqa: B008
    product_image2: UploadFile | None = File(None),  # noqa: B008
    num_variations: int = Form(3),
):
    """Generate glasses virtual try-on images with SSE streaming."""
    try:
        model_image_bytes = await model_image.read()
        glasses_images: list[bytes] = []
        if product_image:
            glasses_images.append(await product_image.read())
        if product_image2:
            glasses_images.append(await product_image2.read())
    except Exception as e:
        logger.error(f"Error reading uploaded files: {e}")
        raise HTTPException(
            status_code=400, detail=f"Error reading uploaded files: {e}"
        ) from e

    if not glasses_images:
        raise HTTPException(
            status_code=400, detail="At least one glasses image is required"
        )

    logger.info(
        f"Generating {num_variations} glasses VTO variations "
        f"with {len(glasses_images)} glasses image(s)"
    )

    async def generate_stream():
        try:
            async for result in run_glasses_vto(
                model_image_bytes,
                glasses_images,
                num_variations,
            ):
                if result.status == "failed" and result.index == -1:
                    yield f"data: {json.dumps({'error': result.error})}\n\n"
                    return
                yield f"data: {json.dumps(result.to_sse_dict())}\n\n"
        except Exception as e:
            logger.error(f"Error in generate_vto_endpoint: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate_stream(), media_type="text/event-stream")


@router.post("/edit-frame")
async def edit_frame_endpoint(
    prompt: str = Form(...),
    generated_image: UploadFile = File(...),  # noqa: B008
):
    """Edits an existing generated frame image based on user prompt."""
    try:
        image_bytes = await generated_image.read()

        edited_frame_bytes = await run_in_threadpool(
            edit_frame_nano, nano_client, prompt, image_bytes
        )

        if not edited_frame_bytes:
            raise Exception("Frame editing failed - no image returned")

        edited_frame_b64 = base64.b64encode(edited_frame_bytes).decode("utf-8")
        return JSONResponse(content={"edited_frame_image": edited_frame_b64})
    except Exception as e:
        logger.error(f"Error in /edit-frame: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Internal Server Error: {e}"
        ) from e
