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

"""POST /api/tryon: image try-on streamed as SSE."""

import functools
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from genmedia4commerce.api.sse import RunForm, read_images, start
from genmedia4commerce.requests import tryon_request
from genmedia4commerce.workflows.tryon.pipeline import run_tryon

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Try-On"])


async def _images(
    product_images: Annotated[list[UploadFile] | None, File()] = None,
    product_image_uris: Annotated[list[str] | None, Form()] = None,
    person_image: Annotated[UploadFile | None, File()] = None,
    person_image_uri: str = Form(""),
    reference_model_image: Annotated[UploadFile | None, File()] = None,
    reference_model_image_uri: str = Form(""),
) -> dict[str, list[bytes | str]]:
    """Each image field as uploads and/or ``gs://`` or https URIs."""
    return {
        "product_images": await read_images(
            "product_images", product_images, product_image_uris, min_count=1
        ),
        "person_image": await read_images(
            "person_image",
            [person_image],
            [person_image_uri],
            min_count=1,
            max_count=1,
        ),
        "reference_model_image": await read_images(
            "reference_model_image",
            [reference_model_image],
            [reference_model_image_uri],
            max_count=1,
        ),
    }


@router.post("/tryon")
async def tryon(
    request: Request,
    run: Annotated[RunForm, Depends()],
    images: Annotated[dict, Depends(_images)],
    style_guidelines: str = Form(""),
    scenario: str = Form(""),
    generate_back: bool = Form(True),
    complete_look: bool = Form(True),
    upscale_crops: bool = Form(False),
    num_variations: int | None = Form(None),
    mode: str = Form(""),
    aspect_ratio: str = Form("3:4"),
    image_size: str = Form("2K"),
) -> StreamingResponse:
    """Try products on a person; streams run/classified/progress/variation events.

    Each image comes as a file or a URI (e.g. from ``POST /api/uploads``).
    ``input`` / ``output``: "inline", "gcs" (kept in the run folder of the
    media bucket; gcs results come as ``media_uri``/``media_url``) or "" for
    the default. With output gcs a ``progress`` event of stage ``record``
    gives the ``manifest`` of the run record (what the images were made
    from), the ``source_run`` of ``POST /api/video-tryon``. Without
    ``num_variations`` / ``mode`` the deployment
    settings apply. ``complete_look``: products only the reference model
    (or a full-length photo of the whole look among the product images)
    shows are cut from it and added. ``upscale_crops``: added shoes are
    upscaled to 2K first.
    """
    build = functools.partial(
        tryon_request,
        style_guidelines=style_guidelines,
        scenario=scenario,
        generate_back=generate_back,
        complete_look=complete_look,
        upscale_crops=upscale_crops,
        num_variations=num_variations,
        mode=mode,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
    )
    return await start(request, run, images, build, run_tryon)
