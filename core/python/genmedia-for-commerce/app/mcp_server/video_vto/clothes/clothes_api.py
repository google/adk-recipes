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

"""REST API endpoint for the clothes video VTO workflow (SSE streaming)."""

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.responses import StreamingResponse
from workflows.video_vto.clothes.pipeline import (
    run_animate_model,
    run_video_vto,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/clothes/video",
    tags=["Clothes Video VTO"],
)


@router.post("/generate-video-vto")
async def generate_video_vto(
    full_body_image: UploadFile = File(...),  # noqa: B008
    garments: list[UploadFile] = File(default=[]),  # noqa: B008
    garment_uris: str = Form(default=""),
    face_image: UploadFile | None = None,
    scenario: str = Form("a plain white studio background"),
    num_variations: int = Form(3),
    number_of_videos: int = Form(4),
    prompt: str = Form(""),
):
    """Full Video VTO pipeline (SSE).

    Runs image VTO internally, picks the best result, then generates videos.
    For video-only (image already ready), use /generate-animate-model instead.
    """
    try:
        full_body_bytes = await full_body_image.read()
        face_bytes = await face_image.read() if face_image else None

        garment_images: list[bytes | str] = [await g.read() for g in garments]
        if garment_uris:
            for uri in json.loads(garment_uris):
                if uri:
                    garment_images.append(uri)
    except Exception as e:
        logger.error(f"Error reading uploaded files: {e}")
        raise HTTPException(
            status_code=400, detail=f"Error reading uploaded files: {e}"
        ) from e

    if not garment_images:
        raise HTTPException(
            status_code=400, detail="At least one garment image is required"
        )

    logger.info(
        f"[VideoVTO] Starting full pipeline: "
        f"{len(garment_images)} garment(s), "
        f"{num_variations} image variations, "
        f"{number_of_videos} videos"
    )

    async def event_stream():
        try:
            async for event in run_video_vto(
                full_body_image=full_body_bytes,
                garment_images=garment_images,
                scenario=scenario,
                num_variations=num_variations,
                face_image=face_bytes,
                number_of_videos=number_of_videos,
                prompt=prompt,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.error(f"[VideoVTO] Pipeline error: {e}", exc_info=True)
            yield f"data: {json.dumps({'status': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/generate-animate-model")
async def generate_animate_model(
    model_image: UploadFile = File(...),  # noqa: B008
    number_of_videos: int = Form(4),
    prompt: str = Form(""),
):
    """Animate a model image into catwalk-style videos (SSE).

    Takes an image of a model already wearing garments and generates
    animation videos. Use this when the model image is already ready
    (e.g. from a previous image VTO result).
    """
    model_image_bytes = await model_image.read()

    logger.info(
        f"[AnimateModel API] Starting: number_of_videos={number_of_videos}"
    )

    async def event_stream():
        try:
            async for event in run_animate_model(
                model_image=model_image_bytes,
                number_of_videos=number_of_videos,
                prompt=prompt,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.error(
                f"[AnimateModel API] Pipeline error: {e}", exc_info=True
            )
            yield f"data: {json.dumps({'status': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
