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

"""The whole-clip check of a try-on video, in one judge call.

The call sees the face crop of the try-on image (when there is one), the
images the video model was given (the try-on image and its back) and about
one frame per second of the whole clip, the stillest of each second
(``pick_frames``). It answers whether the person is the face crop's
(``same_person``) and which frames show what no reference shows
(``invented_frames``): the video model invents what it was never shown, the
back of a person when only the front was given, or a shirt above a
knees-down try-on.
"""

from collections.abc import Sequence

from genmedia4commerce.core import gen, media, video
from genmedia4commerce.core.errors import EvalError
from genmedia4commerce.core.gen import Part, TextParams
from genmedia4commerce.core.models import get_models

__all__ = ["DEADLINE_S", "check_clip", "pick_frames"]

DEADLINE_S = 30
FRAME_SIDE = 768  # the long side of a frame sent

INTRO = (
    "You check a generated fashion video. You see {face}the REFERENCE "
    "images the video model was given (REFERENCE_0 is the try-on image, the "
    "front; REFERENCE_1, when present, the back), then {n} frames of the "
    "video in time order (FRAME_0 to FRAME_{last}, each with its second)."
)
FACE_INTRO = "FACE, a close crop of the person's face, "
IDENTITY = (
    "IDENTITY: decide whether the person in the frames is the person in "
    "FACE: compare the face shape, the features, the skin tone, the hair and "
    "the apparent age wherever the face is visible. Lighting, expression, "
    "head turns, blur and a small face are not differences; glasses may hide "
    "the eyes, so judge from what is visible. same_person is false when the "
    "face in any frame is another person's, or when no frame shows the face "
    "at all. Give face_reason in one short sentence."
)
INVENTED = (
    "INVENTED: list in invented_frames the frames that show a part of the "
    "person or the outfit that NO reference shows, for example: the "
    "person's back when no reference shows the back; the waist, a top or a "
    "shirt when the references show only the legs; the legs when they show "
    "only the upper body. Close shots of things the references show (a "
    "garment, a bag, shoes, the face) are fine. Say in invented_what, in a "
    "few words, what those frames show; an empty list and an empty string "
    "when there are none."
)
_INVENTED_FIELDS = {
    "invented_frames": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    "invented_what": {"type": "STRING"},
}
_FACE_FIELDS = {
    "same_person": {"type": "BOOLEAN"},
    "face_reason": {"type": "STRING"},
}


def _schema(fields: dict) -> dict:
    return {"type": "OBJECT", "properties": fields, "required": list(fields)}


SCHEMA = _schema(_INVENTED_FIELDS)
FACE_SCHEMA = _schema({**_FACE_FIELDS, **_INVENTED_FIELDS})


def pick_frames(clip: bytes) -> list[tuple[float, bytes]]:
    """(second, PNG) of the stillest frame of each second of ``clip``
    (``video.pick_stable_frames``), the long side at most ``FRAME_SIDE``."""
    _, fps, _ = video.probe(clip)
    return [
        (i / fps, media.resize_max(png, FRAME_SIDE))
        for i, png in video.pick_stable_frames(clip, window_s=1.0)
    ]


def _parts(
    frames: Sequence[tuple[float, bytes]],
    front: bytes,
    back: bytes | None,
    face: bytes | None,
) -> list[Part]:
    parts: list[Part] = ["FACE:", face] if face is not None else []
    parts += ["REFERENCE_0 (try-on image, front):", front]
    if back is not None:
        parts += ["REFERENCE_1 (back):", back]
    for k, (second, frame) in enumerate(frames):
        parts += [f"FRAME_{k} ({second:.1f} s):", frame]
    return parts


def check_clip(
    frames: Sequence[tuple[float, bytes]],
    front: bytes,
    back: bytes | None = None,
    face: bytes | None = None,
) -> dict:
    """``{"same_person", "face_reason", "invented_frames",
    "invented_what"}`` of ``frames`` ((second, image), in time order) against
    the try-on image ``front``, its ``back`` and the ``face`` crop.

    Without ``face`` there is no identity question: ``same_person`` is None.
    ``invented_frames`` are indices into ``frames``. A failed call raises
    (``core.gen``); an answer without the fields raises ``EvalError``.
    """
    system = INTRO.format(
        face=FACE_INTRO if face is not None else "",
        n=len(frames),
        last=len(frames) - 1,
    )
    asks = [IDENTITY, INVENTED] if face is not None else [INVENTED]
    raw = gen.generate_json(
        _parts(frames, front, back, face),
        system="\n".join([system, *asks, "Return JSON only."]),
        schema=FACE_SCHEMA if face is not None else SCHEMA,
        params=TextParams(
            model=get_models().judge,
            thinking="LOW",
            media_resolution=gen.MEDIUM_RES,
            deadline_s=DEADLINE_S,
        ),
    )
    if not isinstance(raw, dict):
        raise EvalError(f"clip check: not a JSON object: {raw!r:.200}")
    same = raw.get("same_person") if face is not None else None
    if face is not None and not isinstance(same, bool):
        raise EvalError(f"clip check: no same_person in {raw!r:.200}")
    invented = sorted(
        {
            i
            for i in raw.get("invented_frames") or []
            if isinstance(i, int) and 0 <= i < len(frames)
        }
    )
    reason = raw.get("face_reason") if face is not None else None
    return {
        "same_person": same,
        "face_reason": str(reason or ""),
        "invented_frames": invented,
        "invented_what": str(raw.get("invented_what") or "")
        if invented
        else "",
    }
