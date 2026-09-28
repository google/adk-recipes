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

"""REST API endpoint for the clothes image VTO workflow (SSE streaming)."""

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.responses import StreamingResponse
from workflows.image_vto.clothes.pipeline import run_image_vto

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/clothes",
    tags=["Clothes Image VTO"],
)


@router.post("/generate-vto")
async def generate_vto_endpoint(
    full_body_image: UploadFile = File(...),  # noqa: B008
    garments: list[UploadFile] = File(default=[]),  # noqa: B008
    garment_uris: str = Form(default=""),
    scenario: str = Form("a plain light grey studio environment"),
    num_variations: int = Form(3),
    face_image: UploadFile | None = None,
):
    """Generate virtual try-on images with SSE streaming.

    Returns results progressively as each variation completes.
    """
    try:
        face_image_bytes = await face_image.read() if face_image else None
        full_body_image_bytes = await full_body_image.read()
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
        f"Generating {num_variations} VTO variations with {len(garment_images)} garment(s) "
        f"({sum(1 for g in garment_images if isinstance(g, str))} from GCS)"
    )

    async def generate_stream():
        try:
            async for result in run_image_vto(
                full_body_image_bytes,
                garment_images,
                scenario,
                num_variations,
                face_image_bytes,
            ):
                if result.status == "failed" and result.index == -1:
                    yield f"data: {json.dumps({'error': result.error})}\n\n"
                    return
                yield f"data: {json.dumps(result.to_sse_dict())}\n\n"
        except Exception as e:
            logger.error(f"Error in generate_vto_endpoint: {e}", exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate_stream(), media_type="text/event-stream")
