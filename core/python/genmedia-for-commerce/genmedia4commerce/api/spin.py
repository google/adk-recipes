# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""POST /api/spin: 360° product spin streamed as SSE."""

import functools
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from genmedia4commerce.api.sse import RunForm, read_images, start
from genmedia4commerce.requests import spin_request
from genmedia4commerce.workflows.spin.pipeline import run_spin

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["360 Spin"])


@router.post("/spin")
async def spin(
    request: Request,
    run: Annotated[RunForm, Depends()],
    product_images: Annotated[list[UploadFile] | None, File()] = None,
    product_image_uris: Annotated[list[str] | None, Form()] = None,
    num_variations: int | None = Form(None),
    mode: str = Form(""),
    aspect_ratio: str = Form("auto"),
    resolution: str = Form("720p"),
) -> StreamingResponse:
    """Generate spins (animated WebP); streams run/classified/progress/
    variation events.

    Images, ``input`` and ``output`` as in ``POST /api/tryon``.
    """
    images = {
        "product_images": await read_images(
            "product_images", product_images, product_image_uris, min_count=1
        )
    }
    build = functools.partial(
        spin_request,
        num_variations=num_variations,
        mode=mode,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
    )
    return await start(request, run, images, build, run_spin)
