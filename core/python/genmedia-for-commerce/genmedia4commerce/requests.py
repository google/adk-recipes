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

"""Build and validate workflow requests, and start runs, from plain
tool/REST arguments.

Shared by ``tools.py`` (the Studio agent and jobs) and the REST adapters in
``api/``, so every entry point accepts and rejects the same options and
keeps inputs and results the same way (``core.runs``). Invalid options
and images (wrong type, unreadable, too large, wrong count) raise
``InputError`` before anything is saved or streamed. Images are kept as
sent: crops are cut from the originals, and ``core.gen`` sizes every image
for its model when it calls it.
"""

import asyncio
import dataclasses
import logging
import re
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from typing import TypeVar, cast, get_args

from genmedia4commerce.core import gcs, gen, media, trace
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import Event
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.retry import STALL_RETRIES
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.runs import (
    RUNS_PREFIX,
    RunRef,
    deliver,
    folder_uri,
    load_record,
    new_run,
    parse_mode,
    save_inputs,
)
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Classification
from genmedia4commerce.workflows.spin.pipeline import MAX_IMAGES as MAX_SPIN
from genmedia4commerce.workflows.spin.types import SpinRequest
from genmedia4commerce.workflows.tryon import record
from genmedia4commerce.workflows.tryon.pipeline import MAX_PRODUCT_IMAGES
from genmedia4commerce.workflows.tryon.types import TryOnRequest
from genmedia4commerce.workflows.video_tryon.pipeline import stage_one_image
from genmedia4commerce.workflows.video_tryon.types import (
    Motion,
    VideoTryOnRequest,
)

__all__ = [
    "EVAL_BUDGET_S",
    "IMAGE_SIZES",
    "MAX_VARIATIONS",
    "RUN_MODES",
    "VIDEO_RESOLUTIONS",
    "check_image",
    "spin_request",
    "start_run",
    "tryon_request",
    "video_tryon_request",
]

logger = logging.getLogger(__name__)

RUN_MODES = ("parallel", "sequential")
IMAGE_SIZES = ("1K", "2K", "4K")
VIDEO_RESOLUTIONS = ("720p", "1080p")
MAX_VARIATIONS = 5
# Time a video variation has for its evaluation after the video call.
EVAL_BUDGET_S = 300
R = TypeVar("R")
# Request field -> name of its files in the run's inputs/ folder.
_INPUT_NAMES = {
    "product_images": "product-{}",
    "person_image": "person",
    "reference_model_image": "reference-model",
    "tryon_image": "tryon",
    "back_image": "back",
}


def _choice(field: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise InputError(
            f"{field} must be one of {', '.join(allowed)}; got {value!r}"
        )


def _count(field: str, images: Sequence, low: int, high: int) -> None:
    if not low <= len(images) <= high:
        raise InputError(f"{field}: {low} to {high} images, got {len(images)}")


def _policy(mode: str, num_variations: int | None, **fixed) -> RunPolicy:
    """The deployment run policy with the options the request sets.

    ``mode`` "" and ``num_variations`` None keep the deployment defaults.
    """
    options = dict(fixed)
    if mode:
        _choice("mode", mode, RUN_MODES)
        options["mode"] = mode
    if num_variations is not None:
        if not 1 <= num_variations <= MAX_VARIATIONS:
            raise InputError(
                f"num_variations must be between 1 and {MAX_VARIATIONS}"
            )
        options["num_variations"] = num_variations
    return RunPolicy.from_settings().replace(**options)


def _video_policy(
    mode: str, num_variations: int | None, video: VideoParams
) -> RunPolicy:
    """A video variation may take its video call, asked again once when it
    stalls (``retry.STALL_RETRIES``), plus ``EVAL_BUDGET_S``."""
    calls = (STALL_RETRIES + 1) * video.deadline_s
    return _policy(mode, num_variations, deadline_s=calls + EVAL_BUDGET_S)


def tryon_request(
    product_images: list[bytes],
    person_image: bytes | None,
    *,
    reference_model_image: bytes | None = None,
    complete_look: bool = True,
    upscale_crops: bool = False,
    style_guidelines: str = "",
    scenario: str = "",
    generate_back: bool = True,
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "3:4",
    image_size: str = "2K",
) -> TryOnRequest:
    """Validate the request-level options and build a ``TryOnRequest``."""
    _count("product_images", product_images, 1, MAX_PRODUCT_IMAGES)
    if not person_image:
        raise InputError("a person_image is required")
    _choice("aspect_ratio", aspect_ratio, gen.IMAGE_ASPECT_RATIOS)
    _choice("image_size", image_size, IMAGE_SIZES)
    return TryOnRequest(
        product_images=product_images,
        person_image=person_image,
        reference_model_image=reference_model_image or None,
        complete_look=complete_look,
        upscale_crops=upscale_crops,
        style_guidelines=style_guidelines or None,
        scenario=scenario or None,
        generate_back=generate_back,
        policy=_policy(mode, num_variations),
        image=ImageParams(aspect_ratio=aspect_ratio, image_size=image_size),
    )


def _from_run(
    source_run: str, tryon_image: bytes | None
) -> record.Source | None:
    """What the try-on run ``source_run`` names made ``tryon_image`` from
    (its run record); None when that run kept none."""
    if not tryon_image:
        raise InputError("source_run needs the tryon_image that run made")
    kept = load_record(source_run)
    if kept is None:
        logger.warning(f"no run record in {source_run}; classifying again")
        return None
    return record.read(*kept)


def video_tryon_request(
    *,
    product_images: list[bytes] | None = None,
    person_image: bytes | None = None,
    reference_model_image: bytes | None = None,
    complete_look: bool = True,
    upscale_crops: bool = False,
    style_guidelines: str = "",
    scenario: str = "",
    tryon_image: bytes | None = None,
    back_image: bytes | None = None,
    classification: Classification | None = None,
    source_run: str = "",
    motion: str = "auto",
    prompt: str = "",
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    image_size: str = "2K",
) -> VideoTryOnRequest:
    """Validate the request-level options and build a ``VideoTryOnRequest``.

    Needs a ``tryon_image`` (animate it) or a ``person_image`` and product
    images (image try-on first). ``motion`` "auto" plans the animation from
    the images; "custom" follows ``prompt``, within what they can show.
    ``num_variations`` is the number of try-on images, or with a
    ``tryon_image`` the number of videos. ``source_run`` (with a
    ``tryon_image``) names the try-on run that made it: the products,
    classification and close-ups of its run record replace
    ``product_images`` and ``classification`` (``core.runs.load_record``);
    a run without a record leaves them as they are.
    """
    if not tryon_image and not (person_image and product_images):
        raise InputError(
            "a tryon_image, or a person_image and 1 to "
            f"{MAX_PRODUCT_IMAGES} product_images, is required"
        )
    _count("product_images", product_images or [], 0, MAX_PRODUCT_IMAGES)
    motion = motion or "auto"
    _choice("motion", motion, get_args(Motion))
    if motion == "custom" and not prompt.strip():
        raise InputError("motion 'custom' needs a prompt")
    _choice("aspect_ratio", aspect_ratio, gen.VIDEO_ASPECT_RATIOS)
    _choice("resolution", resolution, VIDEO_RESOLUTIONS)
    _choice("image_size", image_size, IMAGE_SIZES)
    video = VideoParams(aspect_ratio=aspect_ratio, resolution=resolution)
    source = _from_run(source_run, tryon_image) if source_run else None
    if source is not None:
        product_images, classification = source.photos, source.classification
    return VideoTryOnRequest(
        product_images=product_images or [],
        person_image=person_image or None,
        reference_model_image=reference_model_image or None,
        complete_look=complete_look,
        upscale_crops=upscale_crops,
        style_guidelines=style_guidelines or None,
        scenario=scenario or None,
        image=stage_one_image(video, image_size=image_size),
        tryon_image=tryon_image or None,
        back_image=back_image or None,
        classification=classification,
        closeups=source.closeups if source else None,
        motion=cast(Motion, motion),  # checked above
        prompt=prompt or None,
        policy=_video_policy(mode, num_variations, video),
        video=video,
        videos_per_image=num_variations if tryon_image else None,
    )


def spin_request(
    product_images: list[bytes],
    *,
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "auto",
    resolution: str = "720p",
) -> SpinRequest:
    """Validate the request-level options and build a ``SpinRequest``.

    ``aspect_ratio="auto"``: picked from the product's shape (9:16 when it is
    taller than wide).
    """
    _count("product_images", product_images, 1, MAX_SPIN)
    _choice("aspect_ratio", aspect_ratio, ("auto", *gen.VIDEO_ASPECT_RATIOS))
    _choice("resolution", resolution, VIDEO_RESOLUTIONS)
    video = VideoParams(aspect_ratio=aspect_ratio, resolution=resolution)
    return SpinRequest(
        product_images=product_images,
        policy=_video_policy(mode, num_variations, video),
        video=video,
    )


def check_image(data: bytes, field: str) -> bytes:
    """``data`` as a readable image within the limits; HEIC and AVIF become JPEG.

    At most ``gcs.MAX_IMAGE_BYTES`` and 100 MP (``core.media``'s guard).
    PNG, JPEG and WebP come back unchanged unless their EXIF orientation
    needs applying (``media.normalise_image``).
    """
    if len(data) > gcs.MAX_IMAGE_BYTES:
        raise InputError(
            f"{field}: images must be at most {gcs.MAX_IMAGE_BYTES // 2**20} MB"
        )
    if not media.sniff_mime(data).startswith("image/"):
        raise InputError(f"{field}: not a PNG, JPEG, WebP, HEIC or AVIF image")
    try:
        media.image_size(data)
    except InputError as exc:
        raise InputError(f"{field}: {exc}") from exc
    return media.normalise_image(data)


def _input_day(ref: bytes | str, run: RunRef) -> str | None:
    """The date folder of ``ref`` if it is a file of this run's ``inputs/``.

    Any date: an upload just before midnight UTC still belongs to a request
    sent just after it.
    """
    bucket = re.escape(get_settings().media_bucket)
    folder = (
        rf"{RUNS_PREFIX}(\d{{4}}-\d{{2}}-\d{{2}})/{run.session_id}/{run.run_id}"
    )
    found = isinstance(ref, str) and re.fullmatch(
        rf"gs://{bucket}/{folder}/inputs/[^/]+", ref
    )
    return found[1] if found else None


def _in_upload_folder(
    refs: Mapping[str, Sequence[bytes | str]], run: RunRef
) -> RunRef:
    """``run`` dated like its uploaded inputs, so it keeps one folder."""
    days = (_input_day(ref, run) for values in refs.values() for ref in values)
    day = next((day for day in days if day), None)
    return dataclasses.replace(run, date=day) if day else run


def _resolve(
    refs: Mapping[str, Sequence[bytes | str]], run: RunRef
) -> tuple[dict, dict[str, bytes]]:
    """The request's image kwargs, and the named images to save.

    Every image is checked. List fields stay lists; the others
    become bytes or ``None``. Images already in this run's ``inputs/``
    folder (uploaded with this run id) are not saved again.
    """
    kwargs, new = {}, {}
    for field, values in refs.items():
        name = _INPUT_NAMES[field]
        images = [
            check_image(gcs.resolve_image(value), field) for value in values
        ]
        for i, (value, data) in enumerate(zip(values, images, strict=True)):
            trace.save(f"inputs-{field}-{i}", data)
            if not _input_day(value, run):
                new[name.format(i)] = data
        kwargs[field] = images if "{}" in name else next(iter(images), None)
    return kwargs, new


async def start_run(
    refs: Mapping[str, Sequence[bytes | str]],
    build: Callable[..., R],
    workflow: Callable[[R], AsyncGenerator[Event, None]],
    *,
    input: str = "",
    output: str = "",
    run_id: str = "",
    session_id: str = "",
) -> AsyncGenerator[Event, None]:
    """Validate a request, keep its inputs, and return its event stream.

    ``refs`` maps each image field of the request to its images: bytes, or
    ``gs://`` / https references (resolved with ``gcs.resolve_image``).
    ``build(**images)`` builds (and validates) the request, in a worker
    thread (it may read a run record from Cloud Storage). With input mode
    ``gcs`` every image not already in the run's ``inputs/`` folder is saved
    there before the workflow starts (in the date folder of the uploaded
    ones, if any). The stream starts with the run event and delivers the
    media as the output mode says. ``""`` options use the deployment
    defaults. With ``GENMEDIA_DEBUG_DIR`` set the run is traced
    (``core.trace``): its inputs, the request and every step of the stream.
    """
    input_mode = parse_mode(input, name="input") or get_settings().input_mode
    output_mode = parse_mode(output, name="output")
    run = new_run(run_id=run_id, session_id=session_id)
    run = _in_upload_folder(refs, run)
    name = getattr(workflow, "__name__", "").removeprefix("run_")
    token = trace.start(run.run_id, workflow=name, session_id=run.session_id)
    try:
        images, new = await asyncio.to_thread(_resolve, refs, run)
        request = await asyncio.to_thread(build, **images)
        trace.save("request", request)
        info = None
        if input_mode == "gcs":
            await asyncio.to_thread(save_inputs, run, new)
            info = {"inputs": folder_uri(run, "inputs")}
        events = deliver(
            workflow(request), run=run, mode=output_mode, run_info=info
        )
        return trace.scoped(events)
    finally:
        if token is not None:
            trace.TRACE.reset(token)
