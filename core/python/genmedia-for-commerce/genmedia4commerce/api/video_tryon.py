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

"""POST /api/video-tryon: image try-on then video (or animate a given
try-on image), streamed as SSE."""

import functools
import json
from typing import Annotated, get_args

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from genmedia4commerce.api.sse import RunForm, read_images, start
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.types import Classification, Framing, Mode, Product
from genmedia4commerce.requests import video_tryon_request
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon

__all__ = ["router"]

router = APIRouter(prefix="/api", tags=["Video Try-On"])


def _classification(text: str) -> Classification | None:
    """Parse the ``classified`` payload of a try-on run (metadata only)."""
    if not text:
        return None
    try:
        payload = json.loads(text)
        framing, mode = payload["framing"], payload.get("mode", "single")
        products = [
            Product(p["id"], p["category"], p.get("description", ""), refs=[])
            for p in payload.get("products", [])
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise InputError(
            f"classification is not a try-on payload: {exc}"
        ) from exc
    if framing not in get_args(Framing) or mode not in get_args(Mode):
        raise InputError(
            f"classification has an unknown framing or mode: {framing}"
        )
    return Classification(mode=mode, products=products, framing=framing)


async def _images(
    product_images: Annotated[list[UploadFile] | None, File()] = None,
    product_image_uris: Annotated[list[str] | None, Form()] = None,
    person_image: Annotated[UploadFile | None, File()] = None,
    person_image_uri: str = Form(""),
    reference_model_image: Annotated[UploadFile | None, File()] = None,
    reference_model_image_uri: str = Form(""),
    tryon_image: Annotated[UploadFile | None, File()] = None,
    tryon_image_uri: str = Form(""),
    back_image: Annotated[UploadFile | None, File()] = None,
    back_image_uri: str = Form(""),
) -> dict[str, list[bytes | str]]:
    """Each image field as uploads and/or ``gs://`` or https URIs."""
    singles = {
        "person_image": (person_image, person_image_uri),
        "reference_model_image": (
            reference_model_image,
            reference_model_image_uri,
        ),
        "tryon_image": (tryon_image, tryon_image_uri),
        "back_image": (back_image, back_image_uri),
    }
    images = {
        field: await read_images(field, [file], [uri], max_count=1)
        for field, (file, uri) in singles.items()
    }
    images["product_images"] = await read_images(
        "product_images", product_images, product_image_uris
    )
    return images


@router.post("/video-tryon")
async def video_tryon(
    request: Request,
    run: Annotated[RunForm, Depends()],
    images: Annotated[dict, Depends(_images)],
    style_guidelines: str = Form(""),
    scenario: str = Form(""),
    image_size: str = Form("2K"),
    classification: str = Form(""),
    source_run: str = Form(""),
    motion: str = Form("auto"),
    prompt: str = Form(""),
    complete_look: bool = Form(True),
    upscale_crops: bool = Form(False),
    num_variations: int | None = Form(None),
    mode: str = Form(""),
    aspect_ratio: str = Form("9:16"),
    resolution: str = Form("720p"),
) -> StreamingResponse:
    """Dress the person (unless a tryon_image is given), then animate it.

    Streams the run event and the image try-on events (end to end); each
    image that passes is animated at once, its events (back view, selection,
    plan, videos with ``extra.source_index``) interleaved with the others.
    ``num_variations`` is the number of try-on images, each animated image
    getting ``settings.videos_per_image`` videos; with a ``tryon_image`` it
    is the number of videos. ``source_run`` (with a ``tryon_image`` made by
    ``POST /api/tryon`` with output gcs): the ``manifest`` of that run's
    ``record`` event, or any ``gs://`` URI in its run folder; the video
    then uses the products that try-on used, as it classified them, with
    their close-ups, instead of the product images and ``classification``.
    Without it, product images are classified first; with none, the video
    gets no product photo. Images, ``input`` and ``output`` as in
    ``POST /api/tryon``. The video references always go as separate
    images. ``upscale_crops`` as in ``POST /api/tryon``.
    """
    build = functools.partial(
        video_tryon_request,
        style_guidelines=style_guidelines,
        scenario=scenario,
        classification=_classification(classification),
        source_run=source_run,
        motion=motion,
        prompt=prompt,
        complete_look=complete_look,
        upscale_crops=upscale_crops,
        num_variations=num_variations,
        mode=mode,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        image_size=image_size,
    )
    return await start(request, run, images, build, run_video_tryon)
