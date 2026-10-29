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

"""A try-on's photos as the models get them: the product and look photos
cropped to their content, the wearer photo as it is with its face crops
(the prompt's face upscaled with ``GENMEDIA_UPSCALE_FACE``)."""

import asyncio
import dataclasses
import logging

from genmedia4commerce.core import degraded, media, trace
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.workflows.tryon.types import TryOnRequest

__all__ = ["trimmed", "wearer"]

logger = logging.getLogger(__name__)

# The prompt's face is upscaled to 2K (as the close-ups and shoe crops), and
# only when its shorter side is under FACE_MIN_SIDE px: an outfit sends it
# at about 1.2 MP (``cards.IMAGE_PX``) and a single product as it is, so a
# larger crop already gives the model full detail and an upscale would only
# risk changing the face.
FACE_SIZE = "2K"
FACE_MIN_SIDE = 1024


def trimmed(req: TryOnRequest) -> TryOnRequest:
    """``req`` with its product and reference model photos cropped to their
    content (``media.trim``), so the product fills each model's input. The
    person photo is sent as it is."""
    photos = [*req.product_images, req.reference_model_image]
    names = [f"product_images-{k}" for k in range(len(photos) - 1)]
    with ContextPool(max_workers=len(photos)) as pool:
        trimmed = list(pool.map(lambda p: p and media.trim(p), photos))
    for name, before, after in zip(
        [*names, "reference_model_image"], photos, trimmed, strict=True
    ):
        if after is not before:
            trace.save(f"trimmed-{name}", after)
    return dataclasses.replace(
        req, product_images=trimmed[:-1], reference_model_image=trimmed[-1]
    )


def _sharper_face(face: bytes) -> bytes:
    """The prompt's face upscaled by the image model (``FACE_SIZE``, its
    aspect ratio kept, ``media.FACE_UPSCALE_PROMPT``); as it is when
    already large (``FACE_MIN_SIDE``) or when the upscale fails (a
    fallback: ``degraded.report``)."""
    if min(media.image_size(face)) >= FACE_MIN_SIDE:
        return face
    try:
        sharper = media.upscale(
            face, image_size=FACE_SIZE, prompt=media.FACE_UPSCALE_PROMPT
        )
    except Exception as exc:
        logger.warning(f"face upscale failed, keeping the crop: {exc}")
        degraded.report("face upscale")
        return face
    trace.save("face-upscaled", sharper)
    return sharper


async def wearer(
    person: bytes, *, upscale_face: bool = False
) -> tuple[bytes, bytes | None, bytes | None]:
    """Return (body, face crop for the eval, face for the prompt). The body
    is the photo as it is (what the person sits on or uses stays); both
    faces are the one Cloud Vision face box cut from the photo.
    ``upscale_face``: the prompt's face only is upscaled (``_sharper_face``);
    the eval's stays the photo's own. No face found: both faces are None; a
    Cloud Vision error raises."""
    box = await asyncio.to_thread(media.face_box, person)
    if box is None:
        return person, None, None
    face = media.crop_box(person, box, pad=media.FACE_MARGIN)
    if upscale_face:
        return person, face, await asyncio.to_thread(_sharper_face, face)
    return person, face, face
