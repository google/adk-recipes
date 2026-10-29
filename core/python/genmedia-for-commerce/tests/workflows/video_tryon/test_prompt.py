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

"""Tests for the Video Try-On prompt renderer."""

import re

import pytest

from genmedia4commerce.workflows.video_tryon.prompt import FOOTER, render
from genmedia4commerce.workflows.video_tryon.types import (
    Closeup,
    Plan,
    ReferenceSet,
    Shot,
)

IMG = b"img"
BAG = Closeup(3, "bag", "in the person's hand")
SHOES = Closeup(4, "shoes", "on the person's feet")


def refs(framing="full_body", back=1, face=2, closeups=(BAG, SHOES)):
    return ReferenceSet([IMG] * 5, framing, back, face, list(closeups))


def lines(plan, r=None):
    return render(plan, r or refs()).split("\n")


def test_header_names_every_attached_image_where_it_matters():
    header = lines(Plan([Shot("pose", 8.0)]))[0]
    assert header.startswith(
        "[duration=8s] A 8-second fashion video of the person in <IMAGE_0>"
    )
    assert "The face looks exactly like <IMAGE_2>." in header
    assert "The back of the outfit looks exactly like <IMAGE_1>." in header
    assert (
        "<IMAGE_3> is a product photo of the bag, for its exact details: in "
        "the video the bag is always in the person's hand."
    ) in header
    assert "the shoes are always on the person's feet." in header


def test_zoomed_details_are_named_and_used_in_their_close_up():
    zooms = [(5, "padlock on the strap"), (6, "stamp at the bottom")]
    bag = Closeup(3, "bag", "in the person's hand", zooms)
    r = ReferenceSet([IMG] * 7, "full_body", 1, 2, [bag, SHOES])
    out = lines(Plan([Shot("walk", 4.0), Shot("detail", 4.0, "bag")]), r)
    assert (
        "the bag is always in the person's hand. <IMAGE_5> is a close-up of "
        "the bag (padlock on the strap), for its exact shape, colours and "
        "materials. <IMAGE_6> is a close-up of the bag (stamp at the bottom)"
    ) in out[0]
    assert (
        "its details are exactly as in <IMAGE_3>, <IMAGE_5> and <IMAGE_6>;"
    ) in out[2]


def test_one_timestamped_block_per_shot_with_its_transition():
    plan = Plan(
        [
            Shot("walk", 4.0),
            Shot(
                "detail",
                2.0,
                region="bag",
                transition="crossfade",
                action="hand adjusts the strap",  # never on a close-up
            ),
            Shot("pull_out", 4.0, transition="cut"),
        ]
    )
    out = lines(plan)
    assert [line[:11] for line in out[1:4]] == [
        "[00:00.000]",
        "[00:04.000]",
        "[00:06.000]",
    ]
    assert out[1].startswith("[00:00.000] Full body: the person walks")
    assert out[2] == (
        "[00:04.000] Soft crossfade to a new shot: the camera moves slowly "
        "straight in on the bag in the person's hand, with the person's body "
        "and outfit around it in frame and the same side of the bag to the "
        "camera as in <IMAGE_0>; its details are exactly as in <IMAGE_3>; the "
        "person and the bag stay perfectly still, only the camera moves."
    )
    assert out[3].startswith(
        "[00:06.000] Cut to a new shot: the camera moves slowly back"
    )
    assert out[-1] == FOOTER


def test_turn_back_has_two_moments_and_matches_the_back_view():
    plan = Plan([Shot("pose", 4.0), Shot("turn_back", 5.0, action="smiles")])
    out = lines(plan)
    assert out[2].startswith("[00:04.000] The person turns slowly in place")
    assert out[2].endswith(
        "until the back faces the camera, matching <IMAGE_1>."
    )
    assert out[3] == (
        "[00:06.500] The person holds briefly, then keeps turning in the same "
        "direction until facing the camera again, as in <IMAGE_0>."
    )
    assert "smiles" not in "\n".join(out)  # never attached to the turn


def test_timestamps_are_scaled_to_the_whole_seconds_of_the_video():
    # 3.5 + 3.5 + 3.5 = 10.5 s of shots in a 10 s video
    plan = Plan(
        [
            Shot("pose", 3.5),
            Shot("tilt", 3.5, "head", "shoes"),
            Shot("pull_out", 3.5),
        ]
    )
    out = lines(plan)
    assert out[0].startswith("[duration=10s]")
    assert [line[:11] for line in out[1:4]] == [
        "[00:00.000]",
        "[00:03.333]",
        "[00:06.667]",
    ]


def test_a_body_region_is_not_described_as_worn():
    plan = Plan([Shot("push_in", 8.0, region="upper body", action="smiles.")])
    assert lines(plan)[1] == (
        "[00:00.000] The camera moves slowly closer to the upper body, ending "
        "on a medium shot of it, the person facing the camera, smiles."
    )


@pytest.mark.parametrize(
    ("framing", "fills"),
    [
        (
            "footwear",
            "the lower legs and the shoes fill the frame, the knees at its "
            "top edge",
        ),
        (
            "upper_body",
            "the head, the shoulders and the torso down to the waist fill "
            "the frame",
        ),
    ],
)
def test_a_close_framing_is_described_by_what_fills_the_frame(framing, fills):
    plan = Plan([Shot("pose", 4.0), Shot("tilt", 4.0, "head", "waist")])
    out = lines(plan, refs(framing=framing, back=None))
    assert out[0].endswith(
        f"Every shot keeps the close framing of <IMAGE_0>: {fills}."
    )
    assert out[1].startswith(
        f"[00:00.000] Close framing exactly as in <IMAGE_0>: {fills};"
    )
    assert "within this framing" in out[2]


def test_full_body_has_no_framing_sentence():
    r = refs(back=None, face=None, closeups=())
    out = lines(Plan([Shot("pose", 8.0)]), r)
    assert out[0] == (
        "[duration=8s] A 8-second fashion video of the person in <IMAGE_0>, in "
        "the same plain studio with soft, even light. The person, the outfit "
        "and every accessory look exactly as in the reference images, in "
        "every frame."
    )
    assert out[1].startswith(
        "[00:00.000] Full body, from the top of the head to the shoes: "
    )


def test_the_shot_lines_are_affirmative():
    """The video model drops "no"/"never" rules: none in the shots."""
    plan = Plan(
        [
            Shot("pose", 3.0),
            Shot("push_in", 3.0, region="bag", transition="crossfade"),
            Shot("turn_back", 5.0, transition="cut"),
        ]
    )
    for line in lines(plan)[:-1]:  # the footer's "no sound" is the exception
        assert not re.search(r"\b(no|not|never|without|don't)\b", line, re.I), (
            line
        )
