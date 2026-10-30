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

"""A single garment's FRAMING, from where it starts and ends.

The classifier says where a garment starts and ends on a standing person
(``Product.span``, two of ``LANDMARKS``); the code resolves it into one
final instruction. The top edge is the top of the head when the garment
starts at the chest or above, else where it starts (from the waist down:
a frame from one landmark higher drew the whole person, face included);
the
bottom edge is one landmark below its end, the ground (shoes included)
when it ends at the ankles or the feet. The arms and hands are said to
show when the frame holds them: its top edge at the shoulders or above,
its bottom edge at the hips or below. The face shows when the top edge is
the top of the head, the feet when the bottom edge is the ground. Without
a span (any other product: its fixed framing) ``PromptSpec.framing``
decides.
"""

from typing import get_args

from genmedia4commerce.core.types import Landmark
from genmedia4commerce.workflows.tryon.types import (
    FEET_FRAMINGS,
    HEAD_FRAMINGS,
    PromptSpec,
)

__all__ = ["LANDMARKS", "shows_feet", "shows_head", "text"]

LANDMARKS: tuple[Landmark, ...] = get_args(Landmark)  # top to bottom
_HEAD = "the top of the head"
_GROUND = "the ground, shoes included"
_ARMS = "Arms and hands visible, relaxed."
_IN_FRAME = "The product is fully in frame."
_SHOULDERS = LANDMARKS.index("shoulders")
_CHEST = LANDMARKS.index("chest")
_HIPS = LANDMARKS.index("hips")
_ANKLES = LANDMARKS.index("ankles")


def _edges(span: tuple[str, str]) -> tuple[int, int]:
    """The landmarks of the top and bottom edges (0: the top of the head;
    past ``_ANKLES``: the ground)."""
    start, end = (LANDMARKS.index(x) for x in span)
    return (0 if start <= _CHEST else start), end + 1


def _name(index: int) -> str:
    """'the knees', 'mid-thigh'."""
    landmark = LANDMARKS[index]
    return landmark if landmark.startswith("mid-") else f"the {landmark}"


def text(span: tuple[str, str]) -> str:
    """The FRAMING of a garment that starts and ends at ``span``."""
    top, bottom = _edges(span)
    lines = [
        f"From {_HEAD if top == 0 else _name(top)} to "
        f"{_GROUND if bottom > _ANKLES else _name(bottom)}."
    ]
    if top <= _SHOULDERS and bottom >= _HIPS:
        lines.append(_ARMS)
    return " ".join([*lines, _IN_FRAME])


def shows_head(spec: PromptSpec) -> bool:
    """The face is in frame."""
    if spec.span is None:
        return spec.framing in HEAD_FRAMINGS
    return _edges(spec.span)[0] == 0


def shows_feet(spec: PromptSpec) -> bool:
    """The feet are in frame."""
    if spec.span is None:
        return spec.framing in FEET_FRAMINGS
    return _edges(spec.span)[1] > _ANKLES
