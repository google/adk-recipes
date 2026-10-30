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

"""A single garment's FRAMING from where it starts and ends (``framing``),
the same for its back view; accessories keep their fixed framing; every
system text asks the model to state the framing."""

from typing import get_args

import pytest

from genmedia4commerce.core.types import (
    FRAMING_BY_CATEGORY,
    GARMENTS,
    Category,
    Landmark,
)
from genmedia4commerce.workflows.tryon import framing
from genmedia4commerce.workflows.tryon.prompt import FRAMING, build_prompt
from genmedia4commerce.workflows.tryon.styling import NO_FOOTWEAR
from tests.workflows.tryon import test_prompt as tp

ARMS = " Arms and hands visible, relaxed."
IN_FRAME = " The product is fully in frame."
GROUND_SHADOW = "a natural soft shadow on the ground"
ACCESSORIES = [c for c in get_args(Category) if c not in GARMENTS]


def test_the_scale_runs_from_the_head_to_the_feet():
    assert (
        framing.LANDMARKS
        == get_args(Landmark)
        == (
            "head",
            "chin",
            "shoulders",
            "chest",
            "waist",
            "hips",
            "mid-thigh",
            "knees",
            "mid-calf",
            "ankles",
            "feet",
        )
    )


@pytest.mark.parametrize(
    ("span", "text"),
    [
        # the four examples
        (
            ("shoulders", "mid-thigh"),  # bib shorts
            "From the top of the head to the knees." + ARMS + IN_FRAME,
        ),
        (
            ("shoulders", "hips"),  # t-shirt
            "From the top of the head to mid-thigh." + ARMS + IN_FRAME,
        ),
        (
            ("waist", "ankles"),  # trousers
            "From the waist to the ground, shoes included." + IN_FRAME,
        ),
        (
            ("shoulders", "knees"),  # long coat
            "From the top of the head to mid-calf." + ARMS + IN_FRAME,
        ),
        # a garment starting at the chest shows the head too
        (
            ("chest", "hips"),  # tube top
            "From the top of the head to mid-thigh." + ARMS + IN_FRAME,
        ),
        # from the waist down, the frame starts where the garment does
        (("hips", "knees"), "From the hips to mid-calf." + IN_FRAME),
        # the hands hang below a bottom edge above the hips
        (
            ("shoulders", "chest"),  # cropped top
            "From the top of the head to the waist." + IN_FRAME,
        ),
        (("waist", "mid-calf"), "From the waist to the ankles." + IN_FRAME),
        (
            ("chin", "feet"),  # gown
            "From the top of the head to the ground, shoes included."
            + ARMS
            + IN_FRAME,
        ),
    ],
)
def test_the_frame_from_the_garment_span(span, text):
    """One landmark more below; the head from the chest up."""
    assert framing.text(span) == text


def _single(category, span, *, back=False):
    """A single ``category`` product framed by ``span`` (the back view
    with ``back``)."""
    spec = tp._spec([(category, "garment", "flat")])
    spec.framing, spec.span = FRAMING_BY_CATEGORY[category], span
    if back:
        spec.wearer_face, spec.view = None, "back"
        spec.front_result = tp.IMG["front_result"]
    return spec


def _framing(spec) -> str:
    """The FRAMING section's first line."""
    text = tp._text(build_prompt(spec)[1])
    return text.split("## FRAMING\n")[1].splitlines()[0]


@pytest.mark.parametrize("back", [False, True])
def test_a_bib_is_framed_head_to_knees_front_and_back(back):
    """The face shows; the feet do not: no plain shoes, no ground
    shadow, though its category's fixed framing is full body."""
    spec = _single("dress", ("shoulders", "mid-thigh"), back=back)
    text = tp._text(build_prompt(spec)[1])
    assert _framing(spec) == framing.text(("shoulders", "mid-thigh"))
    assert "Identity." in text
    assert NO_FOOTWEAR not in text and GROUND_SHADOW not in text
    assert FRAMING["full_body"] not in text


@pytest.mark.parametrize("back", [False, True])
def test_trousers_are_framed_chest_to_the_ground_front_and_back(back):
    """No face, so no identity rule; plain shoes and their shadow."""
    spec = _single("bottom", ("waist", "ankles"), back=back)
    text = tp._text(build_prompt(spec)[1])
    assert _framing(spec) == (
        "From the waist to the ground, shoes included." + IN_FRAME
    )
    assert "Identity." not in text
    assert NO_FOOTWEAR in text and GROUND_SHADOW in text


@pytest.mark.parametrize("category", ACCESSORIES)
def test_an_accessory_keeps_its_fixed_framing(category):
    spec = _single(category, None)
    assert _framing(spec) == FRAMING[FRAMING_BY_CATEGORY[category]]


def test_the_fixed_framings_are_unchanged():
    assert FRAMING["head"].startswith(
        "Head-and-shoulders portrait, from the top of the head to the chest"
    )
    assert FRAMING["hands"].startswith("Hands and wrists close-up")
    assert FRAMING["footwear"].startswith("From the knees to the ground")
    assert FRAMING["full_body"].startswith(
        "The entire person from above the head to below the feet"
    )
    assert FRAMING["upper_body"] == (
        "From the top of the head to the hips." + ARMS + IN_FRAME
    )


@pytest.mark.parametrize(
    "case", [tp.single_head_front, tp.single_footwear_back, tp.outfit_flat]
)
def test_every_system_text_ends_asking_to_state_the_framing(case):
    """Front views then ask for the pose (the back view has no WEARER BODY
    image to take it from)."""
    system, _ = build_prompt(case())
    framing = (
        " In your generation description, you MUST state the framing: where "
        "the top and the bottom edge of the photo fall, exactly as the "
        "FRAMING section says."
    )
    pose = (
        " In your generation description, you MUST state the wearer's pose "
        "from the WEARER BODY image (standing or seated, and what they sit "
        "on) and keep it."
    )
    chest = (
        " In your generation description, you MUST name what covers the "
        "wearer's chest: a product, or the plain top."
    )
    back = case is tp.single_footwear_back
    assert system.endswith(framing + ("" if back else pose + chest))
