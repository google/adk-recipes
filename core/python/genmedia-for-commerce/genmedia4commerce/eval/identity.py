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

"""Identity in a video: is the person in the frames the one of a face crop?

One judge-model call gets the face crop and a few frames of the video
together and answers ``{same_person, reason}`` (``prompts/video_identity.md``,
schema-constrained).
"""

from collections.abc import Sequence
from pathlib import Path

from genmedia4commerce.core import gen
from genmedia4commerce.core.errors import EvalError
from genmedia4commerce.core.gen import Part, TextParams
from genmedia4commerce.core.models import get_models

__all__ = ["same_person"]

SYSTEM = (
    (Path(__file__).parent / "prompts" / "video_identity.md")
    .read_text(encoding="utf-8")
    .strip()
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "same_person": {"type": "BOOLEAN"},
        "reason": {"type": "STRING"},
    },
    "required": ["same_person", "reason"],
}


def same_person(
    frames: Sequence[bytes], reference_face: bytes
) -> tuple[bool, str]:
    """Whether ``frames`` show the person of ``reference_face``, and why.

    A failed call raises (``core.gen`` has already retried it); an answer
    without ``same_person`` raises ``EvalError``.
    """
    parts: list[Part] = ["FACE:", reference_face]
    for k, frame in enumerate(frames):
        parts += [f"FRAME_{k}:", frame]
    raw = gen.generate_json(
        parts,
        system=SYSTEM,
        schema=SCHEMA,
        params=TextParams(model=get_models().judge, thinking="LOW"),
    )
    same = raw.get("same_person") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or not isinstance(same, bool):
        raise EvalError(f"identity check: no same_person in {raw!r:.200}")
    return same, str(raw.get("reason") or "")
