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

"""Per-frame view labels for videos (one Gemini call)."""

import logging
from collections.abc import Mapping, Sequence
from typing import Literal

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.errors import InputError

__all__ = ["Orientation", "label_frames"]

logger = logging.getLogger(__name__)

Orientation = Literal["shoe", "gemini"]

GEMINI_VIEWS = ("front", "left", "back", "right", "other")
_FRAME_MAX_SIDE = 768  # orientation needs no detail; keeps the request small

# Format with ``n_frames`` and ``n_references``.
GEMINI_PROMPT = (
    "You see {n_frames} frames sampled from a 360° product video and "
    "{n_references} reference photos labelled with their view. For each frame "
    "return the view it shows: front, left, back, right, other."
)
GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "labels": {
            "type": "ARRAY",
            "items": {"type": "STRING", "enum": list(GEMINI_VIEWS)},
        }
    },
    "required": ["labels"],
}


def _gemini_labels(
    frames: Sequence[bytes], references: Mapping[str, bytes] | None
) -> list[str]:
    """One call: references first, then the frames; pads/cleans the answer."""
    references = references or {}
    parts: list[gen.Part] = []
    for view, image in references.items():
        parts += [
            f"REFERENCE {view}:",
            media.resize_max(image, _FRAME_MAX_SIDE),
        ]
    for i, frame in enumerate(frames):
        parts += [f"FRAME {i}:", media.resize_max(frame, _FRAME_MAX_SIDE)]
    raw = gen.generate_json(
        parts,
        system=GEMINI_PROMPT.format(
            n_frames=len(frames), n_references=len(references)
        ),
        schema=GEMINI_SCHEMA,
    )
    labels = raw.get("labels") if isinstance(raw, dict) else None
    labels = [str(x).strip().lower() for x in labels or []][: len(frames)]
    if len(labels) != len(frames):
        logger.warning(
            f"orientation: {len(labels)} labels for {len(frames)} frames"
        )
    labels += ["other"] * (len(frames) - len(labels))
    return [label if label in GEMINI_VIEWS else "other" for label in labels]


def label_frames(
    frames: Sequence[bytes],
    *,
    strategy: Orientation,
    references: Mapping[str, bytes] | None = None,
) -> list[str]:
    """Label each frame with the view it shows.

    ``shoe`` frames are labelled by the shoe spin itself (``shoe_check``),
    never here. ``gemini`` -> front/left/back/right/other (one call, frames
    + references).
    """
    if not frames:
        return []
    if strategy == "gemini":
        return _gemini_labels(frames, references)
    raise InputError(f"unknown orientation strategy {strategy!r}")
