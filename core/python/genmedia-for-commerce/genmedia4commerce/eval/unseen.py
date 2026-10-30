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

"""Blind spots: frames of a video that show what no reference shows.

The video model invents what it was never shown: the back of a person when
only the front was given, or a shirt and shorts above a knees-down try-on.
One text-model call over the references and the frames (2 per second) lists
those frames.
"""

import logging

from genmedia4commerce.core import gen, media, video
from genmedia4commerce.core.gen import Part, TextParams
from genmedia4commerce.core.models import get_models

__all__ = ["unseen_frames"]

logger = logging.getLogger(__name__)

FPS = 2
FRAME_SIDE, REFERENCE_SIDE = 360, 512

SYSTEM = (
    "You see the reference images of a fashion video (REFERENCE_0, ...), then "
    "{n} frames of the video (FRAME_0 to FRAME_{last}). List the frames that "
    "show a part of the person or the outfit that NO reference shows, for "
    "example: the person's back when no reference shows the back; the waist, "
    "a top or a shirt when the references show only the legs; the legs when "
    "they show only the upper body. Close shots of things the references show "
    "(a garment, a bag, shoes, the face) are fine. Return JSON only."
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "unseen_frames": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "what": {"type": "STRING"},
    },
    "required": ["unseen_frames", "what"],
}


def unseen_frames(
    clip: bytes, references: list[bytes]
) -> tuple[list[int], str]:
    """Indices of the frames (at 2 fps) showing what no reference shows,
    and what they show; ``([], "")`` when there are none."""
    frames = video.extract_frames(clip, fps=FPS)
    parts: list[Part] = []
    for i, image in enumerate(references):
        parts += [f"REFERENCE_{i}:", media.resize_max(image, REFERENCE_SIDE)]
    for i, frame in enumerate(frames):
        parts += [f"FRAME_{i}:", media.resize_max(frame, FRAME_SIDE)]
    raw = gen.generate_json(
        parts,
        system=SYSTEM.format(n=len(frames), last=len(frames) - 1),
        schema=SCHEMA,
        params=TextParams(model=get_models().text, thinking="LOW"),
    )
    raw = raw if isinstance(raw, dict) else {}
    found = sorted(
        {
            i
            for i in raw.get("unseen_frames") or []
            if isinstance(i, int) and 0 <= i < len(frames)
        }
    )
    return found, str(raw.get("what") or "") if found else ""
