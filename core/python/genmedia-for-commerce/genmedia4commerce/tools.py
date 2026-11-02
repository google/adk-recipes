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

"""The four tools, used in-process by the Studio agent and its jobs.

Image inputs are base64 strings, ``catalog:`` refs (images of the local
catalogue) or ``gs://`` URIs (``runs/`` in the media bucket); an empty
string means absent. Each generation tool runs one workflow and
folds its events into ``{"variations", "best_index", "ranked_indices",
"classification", "run"}`` (end-to-end video try-on adds
``image_variations`` and ``selected``, one entry per animated image);
``catalog_search`` returns
``{"items"}`` (catalogue rows with their ``ref``). Failures return ``{"error", "retryable"}`` and never raise;
a failure after the run started also carries ``run`` and the
``image_variations`` made so far.
Requests are built, validated and started by ``genmedia4commerce.requests``,
as in the REST adapters: ``input`` and ``output`` choose whether inputs and
results are kept in the run folder of the media bucket (``core.runs``);
gcs results come as ``media_uri`` and ``media_url`` instead of
``media_base64``, and a try-on also keeps its run record: ``run.manifest``
is its URI (null inline), the ``source_run`` of a video of its images. ``stream`` starts the same runs and returns their events
instead of the folded result, for callers that show results as they come.
"""

import asyncio
import base64
import binascii
import contextlib
import functools
import inspect
import logging
from collections.abc import AsyncGenerator, Callable

from genmedia4commerce.core import catalog
from genmedia4commerce.core import catalog_search as search
from genmedia4commerce.core.errors import (
    USER_ERRORS,
    InputError,
    user_message,
)
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    Event,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.requests import (
    spin_request,
    start_run,
    tryon_request,
    video_tryon_request,
)
from genmedia4commerce.workflows.spin.pipeline import run_spin
from genmedia4commerce.workflows.tryon.pipeline import run_tryon
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon

__all__ = ["catalog_search", "spin_360", "stream", "try_on", "video_try_on"]

logger = logging.getLogger(__name__)

_RUN_ARGS = ("input", "output", "run_id", "session_id")


def _ref(value: str, field: str) -> bytes | str:
    """Image bytes from base64; catalogue refs and ``gs://`` / https URIs
    stay references."""
    if value.startswith((catalog.REF_PREFIX, "gs://", "https://")):
        return value
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(f"{field}: not base64 or an image ref") from exc
    if not data:
        raise InputError(f"{field} is empty")
    return data


def _refs(args: dict) -> tuple[dict[str, list[bytes | str]], dict]:
    """Split tool arguments into images and options.

    Each ``<name>_base64`` becomes the list of its images under ``<name>``
    (a string is one image, "" none); the other arguments are options.
    """
    refs, options = {}, {}
    for key, value in args.items():
        name = key.removesuffix("_base64")
        if name == key:
            options[key] = value
        else:
            values = [value] if isinstance(value, str) else value or []
            refs[name] = [_ref(v, key) for v in values if v]
    return refs, options


TOOL_FAILED = "the tool failed on the server (details in its logs)"


def _error(exc: BaseException) -> dict:
    """The result of a failed tool: the message of a user-facing error, a
    generic one (and the details in the log) for any other."""
    if not isinstance(exc, USER_ERRORS):
        logger.exception("tool failed")
    message = user_message(exc, TOOL_FAILED)
    return {"error": message, "retryable": getattr(exc, "retryable", False)}


async def _collect(events: AsyncGenerator[Event, None]) -> dict:
    """Fold a workflow's events into the tool result (spec §3.17).

    The run event's data goes to ``run``, with the ``manifest`` of the
    run record once it is saved (output gcs). Video try-on image-stage
    variations go to ``image_variations`` and the data of each
    ``image_selected`` event (one per animated image) to the ``selected``
    list. An error event ends the result with the error, the run and the
    image variations so far.
    """
    result: dict = {
        "variations": [],
        "best_index": None,
        "ranked_indices": [],
        "classification": None,
    }
    async with contextlib.aclosing(events):
        async for event in events:
            if isinstance(event, ErrorEvent):
                return {
                    "error": event.message,
                    "retryable": event.retryable,
                    "run": result.get("run"),
                    "image_variations": result.get("image_variations", []),
                }
            if isinstance(event, ClassifiedEvent):
                result["classification"] = event.payload
            elif isinstance(event, VariationEvent):
                image = event.extra.get("stage") == "image"
                key = "image_variations" if image else "variations"
                result.setdefault(key, []).append(event.to_dict())
            elif isinstance(event, ProgressEvent):
                if event.stage == "run":
                    result["run"] = dict(event.data)
                elif event.stage == "record" and result.get("run"):
                    result["run"]["manifest"] = event.data.get("manifest")
                elif event.stage == "image_selected":
                    result.setdefault("selected", []).append(event.data)
            elif isinstance(event, CompleteEvent):
                result["best_index"] = event.best_index
                result["ranked_indices"] = event.ranked_indices
    return result


async def _run(
    args: dict,
    build: Callable[..., object],
    workflow: Callable[..., AsyncGenerator[Event, None]],
) -> dict:
    """Run one generation tool; ``args`` are all its arguments (``locals()``).

    ``build`` takes the images and the options by the tool's argument names.
    """
    try:
        return await _collect(await _start(args, build, workflow))
    except Exception as exc:
        return _error(exc)


async def _start(
    args: dict,
    build: Callable[..., object],
    workflow: Callable[..., AsyncGenerator[Event, None]],
) -> AsyncGenerator[Event, None]:
    """Build, check and start one run; return its event stream."""
    run_args = {name: args.pop(name) for name in _RUN_ARGS}
    refs, options = _refs(args)
    return await start_run(
        refs, functools.partial(build, **options), workflow, **run_args
    )


async def try_on(
    product_images_base64: list[str],
    person_image_base64: str,
    reference_model_image_base64: str = "",
    complete_look: bool = True,
    upscale_crops: bool = False,
    style_guidelines: str = "",
    scenario: str = "",
    generate_back: bool = True,
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "3:4",
    image_size: str = "2K",
    input: str = "",
    output: str = "",
    run_id: str = "",
    session_id: str = "",
) -> dict:
    """Photograph a person wearing one or more products (image virtual try-on).

    Use for any wearable: clothes, shoes, glasses, hats, jewellery, bags, or a
    whole outfit. Several photos of one product give a single-product try-on
    (front, plus a back view when a back photo is given); photos of different
    products give an outfit. Returns several evaluated variations, ranked.

    Args:
        product_images_base64: Product photos (1-20): flat shots or photos of
            the product worn by someone. Pass the filenames you were given;
            they are resolved to image data automatically.
        person_image_base64: Photo of the person who will wear the products
            (filename).
        reference_model_image_base64: Optional photo of a model already
            wearing the outfit, used as a styling reference (filename).
        complete_look: With a reference model (or a full-length photo of
            the whole look among the product photos), also dress the person
            in the products only it shows, cut from its photo (e.g. its
            skirt and shoes when only the shirt was uploaded).
        upscale_crops: Upscale the added shoes to 2K first (slower).
        style_guidelines: Optional brand rules, e.g. "jackets closed".
        scenario: Optional background/setting; empty uses the default studio.
        generate_back: Also generate the back view when back photos exist.
        num_variations: How many images to generate (1-5); default: the
            deployment setting (3 unless changed).
        mode: "parallel" (all at once) or "sequential" (stop at the first
            publishable one); "" for the deployment setting (parallel
            unless changed).
        aspect_ratio: Output ratio, e.g. "3:4", "1:1", "9:16".
        image_size: "1K", "2K" or "4K".
        input: "" (default), "inline" or "gcs" (also save the input images
            in this run's folder of the media bucket).
        output: "" (default), "inline" (media as base64) or "gcs" (saved in
            this run's folder; returns "media_uri" and "media_url" instead).
        run_id: Optional 12-character id of an existing run folder to use.
        session_id: Optional id (letters, digits, "_", "-") grouping runs.

    With output "gcs" the run also keeps what its images were made from
    (its products as classified, the photos chosen, their close-ups):
    "run.manifest" is that record, the source_run of video_try_on.
    """
    return await _run(dict(locals()), tryon_request, run_tryon)


async def video_try_on(
    product_images_base64: list[str] | None = None,
    person_image_base64: str = "",
    reference_model_image_base64: str = "",
    complete_look: bool = True,
    upscale_crops: bool = False,
    style_guidelines: str = "",
    scenario: str = "",
    tryon_image_base64: str = "",
    back_image_base64: str = "",
    source_run: str = "",
    motion: str = "auto",
    prompt: str = "",
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    image_size: str = "2K",
    input: str = "",
    output: str = "",
    run_id: str = "",
    session_id: str = "",
) -> dict:
    """Make short videos of a person wearing products (video try-on).

    From product photos and a person photo it dresses the person first (do
    not call try_on before) and animates each image that passes its checks
    (the best one when none does); the images are in "image_variations",
    the animated ones in "selected", each video's extra.source_index names
    its image. Pass tryon_image_base64 only to animate an existing result,
    with source_run when it is a try_on result.

    Args:
        product_images_base64: Product photos (1-20, filenames).
        person_image_base64: The person who will wear them (filename).
        reference_model_image_base64: Optional photo of a model already
            wearing the outfit, used as a styling reference (filename).
        complete_look: With a reference model (or a full-length photo of
            the whole look among the product photos), also dress the person
            in the products only it shows, cut from its photo (e.g. its
            skirt and shoes when only the shirt was uploaded).
        upscale_crops: Upscale the added shoes to 2K first (slower).
        style_guidelines: Optional brand rules, e.g. "jackets closed".
        scenario: Optional setting; empty uses the default studio.
        tryon_image_base64: An existing try-on image to animate (filename);
            skips the image try-on.
        back_image_base64: Its back view (filename), if any; the video then
            turns to show it.
        source_run: With tryon_image_base64 from a try_on result: that
            result's "run.manifest" (or any gs:// URI in its run folder).
            The video then uses the products that try-on used (as it
            classified them, with their close-ups) instead of
            product_images. Without it, product_images are classified
            first; with none, the video gets no product photo.
        motion: "auto" (default: 1 to 3 shots planned from the images,
            e.g. a walk, a close-up of a product, a turn when there is a
            back view) or "custom" (requires prompt).
        prompt: What the video should show when motion is "custom", in
            plain words; parts the images cannot show (e.g. the back without
            a back view) are adapted.
        num_variations: From photos: how many try-on images (1-5); each
            animated image then makes 2 videos (a deployment setting).
            With tryon_image_base64: how many videos (1-5). Default: the
            deployment settings.
        mode: "parallel" or "sequential"; "" for the deployment setting.
        aspect_ratio: Video ratio, "9:16" or "16:9". A 9:16 video also gets
            a 9:16 try-on image; a 16:9 one keeps the 3:4 try-on image.
        resolution: Video resolution, "720p" or "1080p".
        image_size: Try-on image size, "1K", "2K" or "4K".
        input: "" (default), "inline" or "gcs" (also save the input images
            in this run's folder of the media bucket).
        output: "" (default), "inline" (media as base64) or "gcs" (saved in
            this run's folder; returns "media_uri" and "media_url" instead).
        run_id: Optional 12-character id of an existing run folder to use.
        session_id: Optional id (letters, digits, "_", "-") grouping runs.

    """
    return await _run(dict(locals()), video_tryon_request, run_video_tryon)


async def spin_360(
    product_images_base64: list[str],
    num_variations: int | None = None,
    mode: str = "",
    aspect_ratio: str = "auto",
    resolution: str = "720p",
    input: str = "",
    output: str = "",
    run_id: str = "",
    session_id: str = "",
) -> dict:
    """Make a 360-degree spin, an animated WebP, of any product (shoes, bags...).

    Args:
        product_images_base64: Photos of the product from different angles
            (1-8, filenames). More angles give a more faithful spin.
        num_variations: How many spins to generate (1-5); default: the
            deployment setting.
        mode: "parallel" or "sequential" (stop at the first valid spin);
            "" for the deployment setting.
        aspect_ratio: "auto" (default: 9:16 for a product taller than wide,
            else 16:9), "16:9" or "9:16".
        resolution: Video resolution, "720p" or "1080p".
        input: "" (default), "inline" or "gcs" (also save the input images
            in this run's folder of the media bucket).
        output: "" (default), "inline" (media as base64) or "gcs" (saved in
            this run's folder; returns "media_uri" and "media_url" instead).
        run_id: Optional 12-character id of an existing run folder to use.
        session_id: Optional id (letters, digits, "_", "-") grouping runs.

    """
    return await _run(dict(locals()), spin_request, run_spin)


def _generation(tool: str) -> tuple:
    """The tool, request builder and workflow of a generation tool.

    Looked up on each call, so tests can replace the workflows.
    """
    table = {
        "try_on": (try_on, tryon_request, run_tryon),
        "video_try_on": (video_try_on, video_tryon_request, run_video_tryon),
        "spin_360": (spin_360, spin_request, run_spin),
    }
    if tool not in table:
        raise InputError(f"unknown generation tool: {tool!r}")
    return table[tool]


async def stream(tool: str, **args) -> AsyncGenerator[Event, None]:
    """The events of one generation, for callers that show them as they come.

    ``tool`` is "try_on", "video_try_on" or "spin_360"; ``args`` are that
    tool's arguments (its defaults fill the rest), so the request, its checks
    and its run folder are the tool's. An invalid request raises
    (``InputError``, ``TypeError`` for an unknown argument); once the run has
    started, failures arrive as an ``ErrorEvent``.
    """
    fn, build, workflow = _generation(tool)
    bound = inspect.signature(fn).bind(**args)
    bound.apply_defaults()
    return await _start(dict(bound.arguments), build, workflow)


async def catalog_search(query: str, k: int = 5, audience: str = "") -> dict:
    """Search the product catalogue by description; an empty query lists
    the featured products. Each item has a "ref" ("catalog:<id>") that the
    generation tools take as an image.

    Args:
        query: A rich description, e.g. "slim fit dark indigo jeans", or "".
        k: Number of results (1-50).
        audience: Optional "women", "men", "unisex" or a comma-separated mix;
            unisex items are always included.

    """
    try:
        rows = await asyncio.to_thread(
            search.find, query, k=k, audience=audience
        )
    except Exception as exc:
        return _error(exc)
    return {"items": [{**r, "ref": catalog.ref(r["id"])} for r in rows]}
