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

"""The Video Try-On prompt, written from a ``Plan``.

The video model's format: ``[duration=Ns]``, one ``[MM:SS.mmm]`` block per
moment, the attached images named inline as ``<IMAGE_N>`` (0-based, in the
order sent) where they matter, and only affirmative wording (its planner
drops "no"/"never" rules; the blind spots are kept out by ``plan.enforce``).
Product photos are named as detail references and every shot shows the
product worn, or the model cuts to the bare product photo. A framing
narrower than full body is described by what fills the frame, or the model
widens it and invents the rest of the person.

A close-up holds the product still: when a hand moved it, the model turned
the product to a side no photo shows and invented it (a padlock on the
front of a bag photographed three-quarter, 4 times in 4; still: 0 in 3).
"""

from genmedia4commerce.workflows.video_tryon.types import (
    Closeup,
    Plan,
    ReferenceSet,
    Shot,
)

__all__ = ["render"]

FRAMED = {
    "full_body": "Full body, from the top of the head to the shoes",
    "upper_body": "Upper body, from the head to the waist",
    "lower_body": "From the waist to the shoes",
    "footwear": "From the knees to the ground",
    "head": "Head and shoulders",
    "hands": "Close on the hands",
}
FILLS = {  # framings narrower than full body: what fills the frame
    "upper_body": "the head, the shoulders and the torso down to the waist "
    "fill the frame",
    "lower_body": "the legs from the waist down and the shoes fill the frame",
    "footwear": "the lower legs and the shoes fill the frame, the knees at "
    "its top edge",
    "head": "the head and the shoulders fill the frame",
    "hands": "the hands fill the frame",
}
LEADS = {
    "crossfade": "Soft crossfade to a new shot: ",
    "cut": "Cut to a new shot: ",
}
FOOTER = (
    "Only the person and the studio are on screen. "
    "Audio: no sound, completely muted audio track."
)


def _is(name: str) -> str:
    return "are" if name.endswith("s") else "is"


def _header(plan: Plan, refs: ReferenceSet) -> str:
    seconds = plan.duration
    text = (
        f"[duration={seconds}s] A {seconds}-second fashion video of the person "
        "in <IMAGE_0>, in the same plain studio with soft, even light. The "
        "person, the outfit and every accessory look exactly as in the "
        "reference images, in every frame."
    )
    if refs.face is not None:
        text += f" The face looks exactly like <IMAGE_{refs.face}>."
    if refs.back is not None:
        text += (
            f" The back of the outfit looks exactly like <IMAGE_{refs.back}>."
        )
    for c in refs.closeups:
        text += (
            f" <IMAGE_{c.index}> is a product photo of the {c.name}, for its "
            f"exact details: in the video the {c.name} {_is(c.name)} always "
            f"{c.worn}."
        )
        for i, where in c.details:
            text += (
                f" <IMAGE_{i}> is a close-up of the {c.name} ({where}), for "
                "its exact shape, colours and materials."
            )
    if refs.framing in FILLS:
        text += (
            f" Every shot keeps the close framing of <IMAGE_0>: "
            f"{FILLS[refs.framing]}."
        )
    return text


SENTENCES = {  # one per move; turn_back has two (TURN)
    "pose": "{framed}: the person faces the camera exactly as in <IMAGE_0>, "
    "shifts weight naturally and settles into a relaxed pose{extra}.",
    "pose_close": "Close framing exactly as in <IMAGE_0>: {fills}; the person "
    "shifts weight naturally and settles{extra}.",
    "walk": "Full body: the person walks a few natural steps towards the "
    "camera with a confident stride, arms swinging, facing the camera, and "
    "stops closer to it{extra}.",
    "push_in": "The camera moves slowly closer to the {target}, ending on a "
    "medium shot of it{within}{details}, the person facing the camera{extra}.",
    "tilt": "The camera tilts slowly from the {region} to the {to}{within}, "
    "the person facing the camera{extra}.",
    "pull_out": "The camera moves slowly back to the framing of <IMAGE_0>, "
    "the person facing the camera{extra}.",
    "detail": "The camera moves slowly straight in on the {target}, with the "
    "person's body and outfit around it in frame and the same side of the "
    "{region} to the camera as in <IMAGE_0>{details}; the person and the "
    "{region} stay perfectly still, only the camera moves.",
}
TURN = (
    "The person turns slowly in place with small natural steps, led by the "
    "hips and feet, until the back faces the camera, matching <IMAGE_{back}>.",
    "The person holds briefly, then keeps turning in the same direction "
    "until facing the camera again, as in <IMAGE_0>.",
)


def _sentences(shot: Shot, refs: ReferenceSet) -> list[tuple[float, str]]:
    """(seconds into the shot, sentence) for one shot."""
    if shot.move == "turn_back":
        return [
            (0, TURN[0].format(back=refs.back)),
            (shot.seconds / 2, TURN[1]),
        ]
    product: Closeup | None = next(
        (c for c in refs.closeups if c.name == shot.region), None
    )
    close = refs.framing in FILLS
    key = "pose_close" if shot.move == "pose" and close else shot.move
    text = SENTENCES[key].format(
        framed=FRAMED.get(refs.framing, FRAMED["full_body"]),
        fills=FILLS.get(refs.framing, ""),
        extra=f", {shot.action.rstrip('.')}" if shot.action else "",
        within=", within this framing" if close else "",
        target=f"{shot.region} {product.worn}" if product else shot.region,
        details=_details(product),
        region=shot.region,
        to=shot.to_region,
    )
    return [(0, text)]


def _details(product: Closeup | None) -> str:
    if product is None:
        return ""
    names = [f"<IMAGE_{i}>" for i in (product.index, *dict(product.details))]
    if len(names) > 1:  # "<IMAGE_3>, <IMAGE_7> and <IMAGE_8>"
        names = [", ".join(names[:-1]), names[-1]]
    return f"; its details are exactly as in {' and '.join(names)}"


def _stamp(seconds: float) -> str:
    minutes, ms = divmod(round(seconds * 1000), 60_000)
    return f"[{minutes:02d}:{ms // 1000:02d}.{ms % 1000:03d}]"


def render(plan: Plan, refs: ReferenceSet) -> str:
    """The prompt for ``plan`` over ``refs`` (sent in this order)."""
    lines = [_header(plan, refs)]
    scale = plan.duration / sum(s.seconds for s in plan.shots)
    start = 0.0
    for n, shot in enumerate(plan.shots):
        for k, (offset, text) in enumerate(_sentences(shot, refs)):
            lead = LEADS.get(shot.transition, "") if n and not k else ""
            sentence = lead + (text[0].lower() + text[1:] if lead else text)
            lines.append(f"{_stamp(start + offset * scale)} {sentence}")
        start += shot.seconds * scale
    lines.append(FOOTER)
    return "\n".join(lines)
