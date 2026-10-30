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

"""The animation plan: 1 to 3 shots chosen from a menu of moves.

The judge model (medium thinking: 9 s at the median against 17 s for the
text model, never above 12 s in a test) picks the shots from the references
(``auto``) or turns the user's request into shots (``custom``). ``enforce`` then keeps only what the
references can show, so the video never has to invent a blind spot: no turn
without a back view, close shots only of products with a photo, camera moves
only inside the try-on's framing. It also sets the timing: 8 to 10 seconds,
about 4 per shot.
"""

import logging
from typing import get_args

from genmedia4commerce.core import gen
from genmedia4commerce.core.errors import GenMediaError
from genmedia4commerce.core.gen import Part, TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import Framing
from genmedia4commerce.workflows.video_tryon.types import (
    Move,
    Plan,
    ReferenceSet,
    Shot,
    Transition,
)

__all__ = ["MAX_SECONDS", "MIN_SECONDS", "enforce", "plan_animation", "regions"]

logger = logging.getLogger(__name__)

MIN_SECONDS, MAX_SECONDS = 8, 10  # the whole video
SECONDS: dict[str, tuple[float, float]] = {  # one shot, by move
    "detail": (2, 4),
    "turn_back": (5, 6),
}
SHOT_SECONDS = (3, 4)  # the other moves
SINGLE_SECONDS = (MIN_SECONDS, MAX_SECONDS)  # a one-shot video
MAX_SHOTS = 3
MAX_ACTION_WORDS = 12

# Body regions the try-on image shows, by its framing.
REGIONS: dict[Framing, list[str]] = {
    "full_body": ["head", "upper body", "waist", "legs", "shoes"],
    "upper_body": ["head", "upper body", "waist"],
    "lower_body": ["waist", "legs", "shoes"],
    "footwear": ["shoes"],
    "head": ["head"],
    "hands": ["hands"],
}
_TURN_WORDS = ("back", "behind", "away", "turn", "rotat", "spin", "around")

SYSTEM = """You plan a short fashion video of the person in <IMAGE_0> (a virtual try-on), 8 to 10 seconds long, as 1 to 3 shots. Make it attractive and varied, like a premium e-commerce clip.

Moves:
- pose: the person faces the camera in the framing of <IMAGE_0>, with a natural weight shift and a relaxed pose.
- walk: a few steps towards the camera, ending closer (full-body framing only).
- push_in: the camera moves slowly closer to a REGION, ending on a medium shot of it.
- tilt: the camera tilts slowly from one REGION to another REGION.
- pull_out: the camera moves slowly back to the framing of <IMAGE_0>.
- detail: a close shot of a PRODUCT, worn, using its product photo for the details.
- turn_back: the person turns in place to show the back, holds, and turns back to the front.
Transitions into a shot: continuous (one camera move flows into the next), crossfade or cut.

Timing: about 4 seconds per shot (3 to 4); a detail shot can be as short as 2 seconds; turn_back takes 5 seconds; a single shot fills the whole video. For example 4 + 4, 3 + 3 + 4, or 4 + 4 + 2 for a short detail.

The images:
{facts}

Rules: turn_back only when a back view exists (and in automatic mode always include it then). detail only on the PRODUCTS listed. push_in and tilt only between the REGIONS listed. "action" is an optional short extra for a shot other than detail (at most 10 words, e.g. "a light smile") in which the person faces the camera; a detail shot holds the product still.
{mode}
Return JSON only."""

AUTO = "MODE: automatic. Choose the most attractive plan for these images. note: one short sentence describing the plan."
CUSTOM = (
    'MODE: custom. The user asked: "{request}". Follow the request as closely '
    "as the moves and rules allow. When part of it cannot be shown with these "
    "images (for example the back when no back view exists), adapt it and say "
    "how in note, in one short sentence for the user; otherwise note is empty."
)

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "shots": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "move": {"type": "STRING", "enum": list(get_args(Move))},
                    "seconds": {"type": "NUMBER"},
                    "region": {"type": "STRING"},
                    "to_region": {"type": "STRING"},
                    "transition": {
                        "type": "STRING",
                        "enum": list(get_args(Transition)),
                    },
                    "action": {"type": "STRING"},
                },
                "required": ["move", "seconds", "transition"],
            },
        },
        "note": {"type": "STRING"},
    },
    "required": ["shots", "note"],
}


def regions(refs: ReferenceSet) -> list[str]:
    """The body regions the try-on image shows."""
    return REGIONS.get(refs.framing, REGIONS["full_body"])


def _facts(refs: ReferenceSet) -> str:
    back = f"<IMAGE_{refs.back}>" if refs.back is not None else "none"
    lines = [
        "- <IMAGE_0>: the try-on image, framed "
        f"{refs.framing.replace('_', ' ')}.",
        f"- REGIONS shown in <IMAGE_0>: {', '.join(regions(refs))}.",
        f"- Back view: {back}.",
    ]
    if refs.face is not None:
        lines.append(
            f"- <IMAGE_{refs.face}>: the face, for identity (not a shot)."
        )
    lines += [
        f"- PRODUCT '{c.name}': product photo <IMAGE_{c.index}>."
        for c in refs.closeups
    ] or ["- PRODUCTS: none."]
    return "\n".join(lines)


def plan_animation(refs: ReferenceSet, request: str | None = None) -> Plan:
    """Plan the shots for ``refs`` (``request``: the custom motion, if any).

    A failed planner call gives a single pose; ``enforce`` always runs.
    """
    parts: list[Part] = []
    for i, image in enumerate(refs.images):
        parts += [f"<IMAGE_{i}>:", image]
    mode = CUSTOM.format(request=request) if request else AUTO
    try:
        raw = gen.generate_json(
            parts,
            system=SYSTEM.format(facts=_facts(refs), mode=mode),
            schema=SCHEMA,
            params=TextParams(model=get_models().judge, thinking="MEDIUM"),
        )
    except GenMediaError as exc:
        logger.warning(f"animation planner failed: {exc}; a single pose")
        raw = {}
    return enforce(raw if isinstance(raw, dict) else {}, refs)


def _problem(move: str, region: str, to: str, refs: ReferenceSet) -> str:
    """Why ``refs`` cannot show this shot ("" when they can)."""
    shown, products = regions(refs), {c.name for c in refs.closeups}
    if move not in get_args(Move):
        return f"unknown move {move!r}"
    if move == "turn_back" and refs.back is None:
        return "a turn without a back view"
    if move == "detail" and region not in products:
        return f"a close shot of {region!r}, which has no product photo"
    if move == "push_in" and region not in shown and region not in products:
        return f"a push-in on {region!r}, not shown in the try-on"
    if move == "tilt" and not (region in shown and to in shown):
        return f"a tilt from {region!r} to {to!r}, outside the try-on"
    if move == "walk" and refs.framing != "full_body":
        return "a walk without a full-body try-on"
    return ""


def _shot(raw: dict, refs: ReferenceSet, changes: list[str]) -> Shot | None:
    move = str(raw.get("move") or "")
    region = str(raw.get("region") or "").strip().lower()
    to = str(raw.get("to_region") or "").strip().lower()
    if problem := _problem(move, region, to, refs):
        changes.append(f"dropped {move}: {problem}")
        return None
    action = " ".join(str(raw.get("action") or "").split()[:MAX_ACTION_WORDS])
    if move == "detail":  # a moved product turns to sides no photo shows
        action = ""
    if refs.back is None and any(w in action.lower() for w in _TURN_WORDS):
        changes.append(f"dropped the action {action!r}: it could show the back")
        action = ""
    transition = raw.get("transition")
    if transition not in get_args(Transition):
        transition = "continuous"
    try:
        seconds = float(raw.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    return Shot(move, seconds, region, to, transition, action)  # type: ignore[arg-type]


def _timed(shots: list[Shot]) -> None:
    """Each shot into its range, then the total into 8 to 10 seconds."""
    for s in shots:
        low, high = (
            SINGLE_SECONDS
            if len(shots) == 1
            else SECONDS.get(s.move, SHOT_SECONDS)
        )
        s.seconds = min(high, max(low, s.seconds))
    total = sum(s.seconds for s in shots)
    target = min(MAX_SECONDS, max(MIN_SECONDS, total))
    for s in shots:
        s.seconds = round(s.seconds * target / total * 2) / 2  # half seconds
    rounded = sum(s.seconds for s in shots)  # rounding can leave 8 to 10
    shots[-1].seconds += min(MAX_SECONDS, max(MIN_SECONDS, rounded)) - rounded


def enforce(raw: dict, refs: ReferenceSet) -> Plan:
    """The planner's answer, cut to what ``refs`` can show and timed."""
    changes: list[str] = []
    shots = [
        shot
        for item in (raw.get("shots") or [])[:MAX_SHOTS]
        if isinstance(item, dict) and (shot := _shot(item, refs, changes))
    ]
    if not shots:
        shots = [Shot("pose", MIN_SECONDS)]
        changes.append("no shot left: a single pose")
    _timed(shots)
    return Plan(shots, str(raw.get("note") or "").strip(), changes)
