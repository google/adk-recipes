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

"""The model calls the whole repo uses: text, JSON, image, video, embeddings.

Every model parameter is an explicit argument (``TextParams``,
``ImageParams``, ``VideoParams``); nothing is fixed inside a helper. Every
image part is sized for its model when sent (``_parts``): at most 5.16 MP
for the image and text models, the 1280x720 box (padded) for the video
model; embedding inputs are sent unchanged. Call these through the module
(``gen.generate_image(...)``) so tests can fake them.
"""

import functools
import itertools
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy as np
from google.genai import types

from genmedia4commerce.core import _parts, clients, trace
from genmedia4commerce.core._json import UnparsableJSON, parse_json
from genmedia4commerce.core.errors import (
    DeadlineExceeded,
    GenerationError,
    GenMediaError,
    InputError,
)
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import (
    EMPTY_RESULT_ATTEMPTS,
    STALL_RETRIES,
    TRANSPORT_ATTEMPTS,
    Deadline,
    backoff_seconds,
    is_transient,
)

__all__ = [
    "IMAGE_ASPECT_RATIOS",
    "MAX_IMAGES",
    "VIDEO_ASPECT_RATIOS",
    "ImageParams",
    "Part",
    "TextParams",
    "VideoParams",
    "embed",
    "generate_image",
    "generate_json",
    "generate_text",
    "generate_video",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")
# Text, image/video bytes (mime sniffed), or video bytes with sampling
# metadata, e.g. ``(mp4_bytes, {"fps": 2})``.
Part = str | bytes | tuple[bytes, dict]

IMAGE_ASPECT_RATIOS = tuple(
    "auto 1:1 3:4 4:3 9:16 16:9 2:3 3:2 4:5 5:4 21:9".split()
)
VIDEO_ASPECT_RATIOS = ("16:9", "9:16")
MAX_IMAGES = 14  # input images the image model takes in one request
EMBED_DEADLINE_S = 120.0

_HARMS = ("HATE_SPEECH", "DANGEROUS_CONTENT", "SEXUALLY_EXPLICIT", "HARASSMENT")
_SAFETY_OFF = [
    types.SafetySetting(category=f"HARM_CATEGORY_{h}", threshold="OFF")
    for h in _HARMS
]
_RETRYABLE_CODES = tuple(clients.RETRY_OPTIONS.http_status_codes or ())


@dataclass(frozen=True)
class TextParams:
    """Parameters of a text/JSON call. ``model=None`` = registry text model."""

    model: str | None = None
    thinking: str | None = "MEDIUM"  # None=model default, "OFF"=budget 0 (2.5)
    temperature: float = 0.0
    media_resolution: str = "MEDIA_RESOLUTION_HIGH"
    max_output_tokens: int = 32768
    deadline_s: float = 90


@dataclass(frozen=True)
class ImageParams:
    """Parameters of an image call. ``model=None`` = registry image model."""

    model: str | None = None
    aspect_ratio: str = "3:4"
    image_size: str = "2K"
    output_mime_type: str = "image/png"
    thinking: str = "MEDIUM"
    media_resolution: str = "MEDIA_RESOLUTION_HIGH"
    temperature: float = 0.1
    deadline_s: float = 90


@dataclass(frozen=True)
class VideoParams:
    """Parameters of a video call. ``model=None`` = registry video model."""

    model: str | None = None
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    deadline_s: float = 240
    duration: str | None = None  # "3s".."10s" per turn; None = model default


# Shared defaults (frozen, so one instance is safe to reuse as a default).
_TEXT = TextParams()
_IMAGE = ImageParams()
_VIDEO = VideoParams()


def _call(
    fn: Callable[[], T],
    deadline: Deadline,
    what: str,
    rec=None,
    stalls: int = STALL_RETRIES,
) -> T:
    """Run ``fn`` with transport, empty-result and stall retries.

    A call with no answer within ``deadline`` is asked again ``stalls``
    times, each with a fresh deadline of the same length, unless cancelled
    (``retry.CANCEL``). ``rec`` (``trace.call``) records each attempt.
    """
    rec, numbers, stalled = rec or trace.NO_CALL, itertools.count(), 0
    while True:
        try:
            return _attempts(fn, deadline, what, rec, numbers)
        except DeadlineExceeded:
            if deadline.cancelled or stalled >= stalls:
                raise
            stalled += 1
            logger.warning(
                f"{what}: no answer within {deadline.seconds:g}s, "
                f"asked again ({stalled}/{stalls})"
            )
            deadline.restart()


def _attempts(
    fn: Callable[[], T], deadline: Deadline, what: str, rec, numbers
) -> T:
    """Run ``fn`` under ``deadline``, retrying transport and empty results."""
    transport = empty = 0
    while True:
        deadline.check()
        rec.retry = next(numbers)
        started = time.monotonic()
        try:
            result = deadline.run(fn, what)
        except GenMediaError as exc:
            rec.failed(exc)
            raise
        except (_parts.EmptyResult, UnparsableJSON) as exc:
            rec.failed(exc)
            empty += 1
            logger.warning(
                f"{what}: unusable result ({empty}/{EMPTY_RESULT_ATTEMPTS}): {exc}"
            )
            if empty >= EMPTY_RESULT_ATTEMPTS:
                raise GenerationError(f"{what}: {exc}") from exc
        except Exception as exc:
            rec.failed(exc)
            if not is_transient(exc):
                retryable = getattr(exc, "code", None) in _RETRYABLE_CODES
                raise GenerationError(
                    f"{what} failed: {exc}", retryable=retryable
                ) from exc
            transport += 1
            logger.warning(
                f"{what}: transport error ({transport}/{TRANSPORT_ATTEMPTS}): {exc}"
            )
            if transport >= TRANSPORT_ATTEMPTS:
                raise GenerationError(
                    f"{what} failed: {exc}", retryable=True
                ) from exc
            if not deadline.sleep(backoff_seconds(transport)):
                raise DeadlineExceeded(f"{what}: deadline exceeded") from exc
        else:
            elapsed = time.monotonic() - started
            rec.done(result, duration_ms=round(elapsed * 1000))
            return result


def _check_ratio(aspect_ratio: str, allowed: tuple[str, ...]) -> None:
    if aspect_ratio not in allowed:
        raise InputError(f"aspect_ratio {aspect_ratio!r} not in {allowed}")


_ENDPOINT = re.compile(r"^projects/[^/]+/locations/([a-z0-9-]+)/endpoints/")


def _generate(
    model: str,
    parts: Sequence[Part],
    deadline_s: float,
    extract: Callable[[Any], T],
    **config: Any,
) -> T:
    deadline = Deadline(deadline_s)
    # Tuned models live on regional endpoints: call them in their region.
    region = _ENDPOINT.match(model)
    client = clients.get_client(location=region[1] if region else None)
    parts = _parts.fit_parts(parts)
    contents = [types.Content(role="user", parts=_parts.to_parts(parts))]
    system = config.get("system_instruction")
    params = {k: v for k, v in config.items() if k != "system_instruction"}
    with trace.call(model, parts, system=system, params=params) as rec:

        def attempt() -> T:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    **config,
                    safety_settings=_SAFETY_OFF,
                    http_options=types.HttpOptions(
                        timeout=deadline.remaining_ms
                    ),
                ),
            )
            rec.finish = _parts.response_parts(response)[1]
            return extract(response)

        return _call(attempt, deadline, what=model, rec=rec)


def _thinking(level: str | None) -> types.ThinkingConfig | None:
    if level == "OFF":
        return types.ThinkingConfig(thinking_budget=0)
    return types.ThinkingConfig(thinking_level=level) if level else None


def _text_call(parts, system, params: TextParams, extract, **extra: Any):
    return _generate(
        params.model or get_models().text,
        parts,
        params.deadline_s,
        extract,
        temperature=params.temperature,
        max_output_tokens=params.max_output_tokens,
        thinking_config=_thinking(params.thinking),
        media_resolution=params.media_resolution,
        system_instruction=system,
        **extra,
    )


def generate_text(
    parts: Sequence[Part],
    *,
    system: str | None = None,
    params: TextParams = _TEXT,
) -> str:
    """Generate text; an empty answer is retried once, then GenerationError."""
    return _text_call(parts, system, params, _parts.response_text)


def generate_json(
    parts: Sequence[Part],
    *,
    system: str | None = None,
    schema: dict | type | None = None,
    params: TextParams = _TEXT,
) -> dict | list:
    """Generate JSON (optionally schema-constrained), parsed leniently."""
    return _text_call(
        parts,
        system,
        params,
        lambda response: parse_json(_parts.response_text(response)),
        response_mime_type="application/json",
        response_schema=schema,
    )


def generate_image(
    parts: Sequence[Part],
    *,
    system: str | None = None,
    params: ImageParams = _IMAGE,
) -> bytes:
    """Generate one image; ``aspect_ratio="auto"`` keeps the input's ratio."""
    _check_ratio(params.aspect_ratio, IMAGE_ASPECT_RATIOS)
    return _generate(
        params.model or get_models().image,
        parts,
        params.deadline_s,
        _parts.response_image,
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=None
            if params.aspect_ratio == "auto"
            else params.aspect_ratio,
            image_size=params.image_size,
            output_mime_type=params.output_mime_type,
        ),
        thinking_config=_thinking(params.thinking),
        media_resolution=params.media_resolution,
        temperature=params.temperature,
        system_instruction=system,
    )


def generate_video(
    images: Sequence[bytes],
    prompt: str,
    *,
    params: VideoParams = _VIDEO,
    store: bool = False,
    extends: str | None = None,
) -> bytes:
    """One video (MP4) from reference images and a prompt. ``store`` keeps the
    interaction; ``extends`` continues a stored one and returns the whole
    video. Both return a ``core.types.StoredVideo`` (bytes with its id)."""
    _check_ratio(params.aspect_ratio, VIDEO_ASPECT_RATIOS)
    models = get_models()
    model = params.model or models.video
    deadline = Deadline(params.deadline_s)
    client = clients.get_client(
        location="global", api_revision=models.video_api_revision
    )
    sent, turn = _parts.video_inputs(prompt, images, params.aspect_ratio)
    response_format = _parts.video_format(params)

    def attempt() -> bytes:
        video = _parts.create_video(
            client.interactions,
            model=model,
            turn=turn,
            response_format=response_format,
            timeout=deadline.remaining,
            store=store,
            extends=extends,
        )
        if video is None:
            raise _parts.EmptyResult("no video in the model output")
        return video

    with trace.call(model, [prompt, *sent], params=params) as rec:
        return _call(attempt, deadline, what=model, rec=rec)


def embed(
    items: Sequence[Part | Sequence[Part]],
    *,
    model: str,
    location: str | None = None,
    output_dims: int | None = None,
) -> np.ndarray:
    """Embed each item separately; returns shape ``(len(items), dims)``.

    An item is one part, or a sequence of parts embedded together as one
    multi-part content (e.g. ``[instruction_text, image]``).
    """
    if not items:
        raise InputError("embed needs at least one item")
    deadline = Deadline(EMBED_DEADLINE_S)
    client = clients.get_client(location=location)

    def attempt(item: Part | Sequence[Part]) -> list:
        config = types.EmbedContentConfig(
            output_dimensionality=output_dims,
            http_options=types.HttpOptions(timeout=deadline.remaining_ms),
        )
        result = client.models.embed_content(
            model=model, contents=[_parts.to_content(item)], config=config
        )
        if not result.embeddings or not result.embeddings[0].values:
            raise _parts.EmptyResult("no embedding returned")
        return result.embeddings[0].values

    vectors = [  # one deadline for every item: not restarted
        _call(functools.partial(attempt, i), deadline, what=model, stalls=0)
        for i in items
    ]
    out = np.asarray(vectors, dtype=np.float32)
    trace.save("embed", None, meta={"model": model, "shape": out.shape})
    return out
