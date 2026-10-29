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

"""Tests for the animation plan: the planner call, the blind-spot rules and
the timing."""

import pytest

from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.workflows.video_tryon.plan import (
    enforce,
    plan_animation,
)
from genmedia4commerce.workflows.video_tryon.types import (
    Closeup,
    Plan,
    ReferenceSet,
    Shot,
)

BAG = Closeup(2, "bag", "in the person's hand")


def refs(framing="full_body", back=None, closeups=(BAG,)):
    images = [b"try-on", b"face", b"bag"] + ([b"back"] if back else [])
    return ReferenceSet(images, framing, back, 1, list(closeups))


def shot(move, seconds=4, **kw):
    return {"move": move, "seconds": seconds, "transition": "cut", **kw}


def moves(plan):
    return [(s.move, s.seconds) for s in plan.shots]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([shot("pose"), shot("walk")], [("pose", 4), ("walk", 4)]),
        (
            [shot("walk", 3), shot("pose", 3), shot("pull_out", 4)],
            [("walk", 3), ("pose", 3), ("pull_out", 4)],
        ),
        (
            [shot("walk"), shot("pose"), shot("detail", 2, region="bag")],
            [("walk", 4), ("pose", 4), ("detail", 2)],
        ),
    ],
    ids=["4+4", "3+3+4", "4+4+2"],
)
def test_the_planned_timing_is_kept(raw, expected):
    assert moves(enforce({"shots": raw}, refs())) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([shot("pose", 3.5), shot("walk", 3.5)], [("pose", 4), ("walk", 4)]),
        # clamped to 4 + 3, then stretched to 8 in proportion
        ([shot("pose", 9), shot("walk", 1)], [("pose", 4.5), ("walk", 3.5)]),
        (
            [shot("pose", 6), shot("walk", 6), shot("pull_out", 6)],
            [("pose", 3.5), ("walk", 3.5), ("pull_out", 3.0)],
        ),
        ([shot("pose", 5)], [("pose", 8)]),
        ([shot("pose", 30)], [("pose", 10)]),
        ([shot("pose", "soon")], [("pose", 8)]),
    ],
    ids=["short", "uneven", "long", "one-short", "one-long", "not-a-number"],
)
def test_the_timing_is_fixed_into_8_to_10_seconds(raw, expected):
    plan = enforce({"shots": raw}, refs())
    assert moves(plan) == expected
    assert 8 <= sum(s.seconds for s in plan.shots) <= 10
    assert plan.duration == round(sum(s.seconds for s in plan.shots))


def test_a_turn_takes_at_least_five_seconds():
    raw = [shot("turn_back", 3), shot("detail", 3, region="bag")]
    plan = enforce({"shots": raw}, refs(back=3))
    assert moves(plan) == [("turn_back", 5), ("detail", 3)]
    assert plan.shows_back


@pytest.mark.parametrize(
    ("framing", "back", "raw_shot", "why"),
    [
        ("full_body", None, shot("turn_back"), "a turn without a back view"),
        ("full_body", None, shot("detail", region="shoes"), "no product photo"),
        ("upper_body", None, shot("push_in", region="shoes"), "not shown"),
        (
            "upper_body",
            None,
            shot("tilt", region="head", to_region="shoes"),
            "outside",
        ),
        ("upper_body", None, shot("walk"), "without a full-body try-on"),
        ("full_body", None, shot("moonwalk"), "unknown move"),
    ],
)
def test_shots_the_references_cannot_show_are_dropped(
    framing, back, raw_shot, why
):
    plan = enforce({"shots": [shot("pose"), raw_shot]}, refs(framing, back))
    assert [s.move for s in plan.shots] == ["pose"]
    assert plan.shots[0].seconds == 8  # alone, it fills the video
    (change,) = plan.changes
    assert why in change


def test_allowed_moves_are_kept_with_their_regions():
    raw = [
        shot("push_in", 3, region="Upper Body"),
        shot("tilt", 3, region="head", to_region="shoes"),
        shot("detail", 2, region="bag"),
    ]
    plan = enforce({"shots": raw, "note": " a plan "}, refs())
    assert [(s.move, s.region, s.to_region) for s in plan.shots] == [
        ("push_in", "upper body", ""),
        ("tilt", "head", "shoes"),
        ("detail", "bag", ""),
    ]
    assert (plan.note, plan.changes) == ("a plan", [])


def test_a_close_up_never_has_an_action():
    raw = [shot("detail", region="bag", action="the hand lifts the bag")]
    assert enforce({"shots": raw}, refs()).shots[0].action == ""


def test_an_action_that_could_turn_is_dropped_without_a_back_view():
    raw = [shot("pose", 8, action="turns around to the back")]
    plan = enforce({"shots": raw}, refs())
    assert plan.shots[0].action == ""
    assert "could show the back" in plan.changes[0]
    plan = enforce({"shots": raw}, refs(back=3))
    assert plan.shots[0].action == "turns around to the back"


def test_at_most_three_shots_and_a_valid_transition():
    raw = [shot("pose", 3, transition="wipe")] + [shot("walk", 3)] * 3
    plan = enforce({"shots": raw}, refs())
    assert len(plan.shots) == 3
    assert plan.shots[0].transition == "continuous"


@pytest.mark.parametrize(
    "raw", [{}, {"shots": []}, {"shots": [shot("turn_back")]}]
)
def test_nothing_valid_is_a_single_pose(raw):
    plan = enforce(raw, refs())
    assert moves(plan) == [("pose", 8)]


def test_the_planner_sees_the_images_and_what_they_allow(fake_gen):
    fake_gen.json(
        {"shots": [shot("walk"), shot("detail", region="bag")], "note": "x"}
    )
    plan = plan_animation(refs(back=3))
    assert isinstance(plan, Plan) and moves(plan) == [
        ("walk", 4),
        ("detail", 4),
    ]
    (call,) = fake_gen.calls
    assert call["params"].model == get_models().judge
    assert call["params"].thinking == "MEDIUM"
    assert call["raw_parts"] == [
        "<IMAGE_0>:",
        b"try-on",
        "<IMAGE_1>:",
        b"face",
        "<IMAGE_2>:",
        b"bag",
        "<IMAGE_3>:",
        b"back",
    ]
    system = call["system"]
    assert (
        "REGIONS shown in <IMAGE_0>: head, upper body, waist, legs, shoes."
        in system
    )
    assert "- Back view: <IMAGE_3>." in system
    assert "- PRODUCT 'bag': product photo <IMAGE_2>." in system
    assert "MODE: automatic" in system


def test_a_custom_request_reaches_the_planner(fake_gen):
    fake_gen.json({"shots": [shot("pose", 8)], "note": "No back photo."})
    plan = plan_animation(refs(), "show the back")
    assert 'The user asked: "show the back"' in fake_gen.calls[0]["system"]
    assert "- Back view: none." in fake_gen.calls[0]["system"]
    assert plan.note == "No back photo."


def test_a_failed_planner_is_a_single_pose(fake_gen):
    fake_gen.json(GenerationError("planner down"))
    assert moves(plan_animation(refs())) == [("pose", 8)]


def test_plan_summary_duration_and_back():
    plan = Plan([Shot("walk", 4), Shot("detail", 2.5, region="bag")])
    assert plan.summary() == "walk → detail (bag), 6 s"
    assert not plan.shows_back
