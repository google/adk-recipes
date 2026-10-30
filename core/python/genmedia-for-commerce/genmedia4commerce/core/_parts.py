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

"""SDK request parts and response helpers (private to ``core.gen``).

Images are sized for each model when they are sent (``media.fit``), so the
model gets the pixels it would keep anyway, scaled with Lanczos on our side.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

from google.genai import types

from genmedia4commerce.core import media
from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.types import StoredVideo

if TYPE_CHECKING:
    from genmedia4commerce.core.gen import Part

__all__ = [
    "CONTENT_FIT",
    "VIDEO_BOXES",
    "EmptyResult",
    "create_video",
    "fit_parts",
    "response_image",
    "response_parts",
    "response_text",
    "to_content",
    "to_parts",
    "video_bytes",
    "video_format",
    "video_inputs",
]

# The image and text models scale larger images down to 5.16 MP at most,
# sides aligned to multiples of 16, keeping the aspect ratio.
CONTENT_FIT: dict[str, Any] = {"max_pixels": 5_160_960, "multiple": 16}
# The video model scales each reference image to fit this box and pads the
# rest with black; we pad with the image's own background instead, and send
# PNG (a 720p reference is ~250 KB; JPEG 95 shifted small text edges).
VIDEO_BOXES = {"16:9": (1280, 720), "9:16": (720, 1280)}


def _is_video(value: Any) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], bytes)
        and isinstance(value[1], dict)
    )


def _is_part(value: Any) -> bool:
    return isinstance(value, str | bytes) or _is_video(value)


def _to_part(p: Part) -> types.Part:
    if isinstance(p, str):
        return types.Part.from_text(text=p)
    if isinstance(p, bytes):
        return types.Part.from_bytes(data=p, mime_type=media.sniff_mime(p))
    if _is_video(p):
        data, metadata = p
        return types.Part(
            inline_data=types.Blob(data=data, mime_type=media.sniff_mime(data)),
            video_metadata=types.VideoMetadata(**metadata),
        )
    raise InputError(f"unsupported part: {type(p).__name__}")


def fit_parts(parts: Sequence[Part]) -> list[Part]:
    """``parts`` with every image sized for the image and text models."""
    return [
        media.fit(p, **CONTENT_FIT)
        if isinstance(p, bytes) and media.sniff_mime(p).startswith("image/")
        else p
        for p in parts
    ]


def video_inputs(
    prompt: str, images: Sequence[bytes], aspect_ratio: str
) -> tuple[list[bytes], list[dict]]:
    """The images sized for the video model, and the ``interactions`` input.

    The input is the prompt, then each image (base64).
    """
    box = VIDEO_BOXES[aspect_ratio]
    sent = [
        media.fit(image, box=box, pad=True, lossless=True) for image in images
    ]
    inputs: list[dict] = [{"type": "text", "text": prompt}]
    inputs += [
        {
            "type": "image",
            "data": base64.b64encode(image).decode("ascii"),
            "mime_type": media.sniff_mime(image),
        }
        for image in sent
    ]
    return sent, inputs


def to_parts(parts: Sequence[Part]) -> list[types.Part]:
    """Convert every part of one request."""
    return [_to_part(p) for p in parts]


def to_content(item: Part | Sequence[Part]) -> types.Content:
    """One user content: a single part, or a sequence of parts together."""
    parts = (
        [cast("Part", item)]
        if _is_part(item)
        else list(cast("Sequence[Part]", item))
    )
    return types.Content(role="user", parts=to_parts(parts))


class EmptyResult(Exception):
    """The model answered but returned nothing usable."""


def response_parts(response: Any) -> tuple[list, Any]:
    """The first candidate's parts (thoughts left out) and finish reason."""
    candidate = response.candidates[0] if response.candidates else None
    content = candidate.content if candidate else None
    parts = [
        p for p in (content.parts if content else None) or [] if not p.thought
    ]
    return parts, candidate.finish_reason if candidate else None


def response_text(response: Any) -> str:
    """The answer's text; ``EmptyResult`` when there is none."""
    parts, finish = response_parts(response)
    text = "".join(p.text for p in parts if p.text).strip()
    if not text:
        raise EmptyResult(f"no text (finish_reason={finish})")
    return text


def response_image(response: Any) -> bytes:
    """The answer's first inline image; ``EmptyResult`` when there is none."""
    parts, finish = response_parts(response)
    for p in parts:
        if p.inline_data and p.inline_data.data:
            return p.inline_data.data
    raise EmptyResult(f"no image part (finish_reason={finish})")


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a dict or an SDK/pydantic object (extras included)."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def video_format(params: Any) -> dict:
    """The ``response_format`` of a video call (``params``: ``VideoParams``)."""
    fmt = {"type": "video", "aspect_ratio": params.aspect_ratio}
    fmt["resolution"] = params.resolution
    if params.duration:
        fmt["duration"] = params.duration
    return fmt


def create_video(
    interactions: Any,
    *,
    model: str,
    turn: list[dict],
    response_format: dict,
    timeout: float,
    store: bool = False,
    extends: str | None = None,
) -> bytes | None:
    """One video turn; a stored or continued turn returns a ``StoredVideo``.

    ``extends`` continues a stored interaction (``previous_interaction_id``):
    its video is returned whole, the new part appended.
    """
    extra: dict = {"store": True} if store or extends else {}
    if extends:
        extra["previous_interaction_id"] = extends
    interaction = interactions.create(
        model=model,
        input=turn,
        timeout=timeout,
        response_format=response_format,
        **extra,
    )
    video = video_bytes(interaction)
    if video is None or not extra:
        return video
    return StoredVideo(video, _field(interaction, "id"), extends)


def video_bytes(interaction: Any) -> bytes | None:
    """Return the inline video of an ``interactions.create`` result, or None."""
    parts: list = []
    for step in _field(interaction, "steps") or []:
        if _field(step, "type") == "model_output":
            parts.extend(_field(step, "content") or [])
    parts.extend(_field(interaction, "outputs") or [])
    for part in parts:
        if _field(part, "type") != "video":
            continue
        data = _field(part, "data")
        if data:
            return base64.b64decode(data) if isinstance(data, str) else data
        uri = _field(part, "uri")
        if uri:
            raise GenerationError(
                f"video model returned a URI instead of inline video ({uri})"
            )
    return None
