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

import pytest

from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import video_fidelity
from genmedia4commerce.workflows.spin import shoe_check, shoe_refs
from tests.workflows.spin.conftest import clip

R, FR, F, FL, L, BL, B, BR = shoe_check.STANDING_VIEWS
TURN = [R, FR, F, FL, L, BL, B, BR, R]  # one clockwise turn, a view a sample


def _read(labels):
    return shoe_check.read_turn(labels, [k / 2 for k in range(len(labels))])


# ---------------------------------------------------------------- reading


def test_a_clean_clockwise_turn():
    reading = _read([R, R, *TURN[1:], R])
    assert reading.clockwise and reading.problems == []
    assert reading.steps == 8 and reading.back_at == 9


def test_an_anticlockwise_turn_is_a_clean_turn_the_other_way():
    reading = _read(TURN[::-1])
    assert not reading.clockwise and reading.problems == []


def test_turning_back_is_a_problem():
    reading = _read([R, FR, F, FR, F, FL, L, BL, B, BR, R])
    assert reading.problems == ["turns back at 1.5 s"]


def test_two_views_in_half_a_second_is_a_jump():
    reading = _read([R, FR, FL, L, BL, B, BR, R])
    assert reading.problems == ["jumps 90 degrees at 1.0 s"]


def test_half_a_turn_in_half_a_second_is_a_jump_either_way():
    assert "jumps 180 degrees at 0.5 s" in _read([R, L, BL, B, BR, R]).problems


def test_a_shoe_not_standing_is_a_problem():
    reading = _read([R, FR, "not_standing", F, FL, L, BL, B, BR, R])
    assert reading.problems == ["the shoe is not standing on its sole at 1.0 s"]


def test_an_incomplete_turn_is_a_problem():
    reading = _read(TURN[:-2])  # ends on back
    assert reading.problems == ["turns 270 degrees, not a full turn"]
    assert reading.back_at is None


def test_too_many_samples_without_a_view():
    labels = [R, *["missing"] * 4, *TURN[1:]]  # more than a quarter
    assert "4 of 13 frames have no view" in _read(labels).problems


def test_no_view_at_all():
    problems = _read(["missing"] * 4).problems
    assert "no frame shows the shoe's view" in problems


def test_samples_every_half_second_and_the_last_frame():
    assert shoe_check._samples(50, 24) == [0, 12, 24, 36, 48, 49]
    assert shoe_check._samples(49, 24) == [0, 12, 24, 36, 48]


# ---------------------------------------------------------------- the clip


def _labelled(monkeypatch, labels):
    calls = []

    def label(images, eval_mode=False):
        calls.append((len(images), eval_mode))
        return list(labels)

    monkeypatch.setattr(shoe_refs, "label_images", label)
    return calls


def test_a_turn_back_at_the_start_is_cut_on_the_frame_most_like_the_first(
    monkeypatch,
):
    # 9 samples (0, 2, ..., 16) + the last (17); frame 15 repeats frame 0.
    values = [*range(15), 0, 98, 99]
    calls = _labelled(monkeypatch, [*TURN, R])
    turn = shoe_check.check_turn(clip(values, fps=4))
    assert calls == [(10, True)]  # one call, eval mode
    assert turn.problems == [] and turn.details["cut_frame"] == 16
    assert core_video.probe(turn.clip)[0] == 16
    assert [i for i, _ in turn.frames] == [0, 2, 4, 6, 8, 10, 12, 14]
    assert turn.labels == TURN[:8]


def test_an_anticlockwise_clip_is_reversed(monkeypatch):
    original = clip(list(range(18)), fps=4)
    _labelled(monkeypatch, [*TURN[::-1], R])  # 0..16, then 17
    turn = shoe_check.check_turn(original)
    assert turn.details["reversed"] and turn.problems == []
    assert turn.labels[:3] == [R, R, FR]  # the original's last two first
    assert turn.clip != original
    assert [i for i, _ in turn.frames][:2] == [0, 1]


def test_a_rejected_turn_keeps_the_whole_clip(monkeypatch):
    original = clip(list(range(18)), fps=4)
    _labelled(monkeypatch, TURN[:6] + [R] * 4)  # back from back_left to right
    turn = shoe_check.check_turn(original)
    assert turn.problems and turn.clip == original
    assert "cut_frame" not in turn.details


# ---------------------------------------------------------------- evaluate

REFS = {"right": b"side", "back_right": b"back", "top_front": b"top"}


@pytest.fixture
def turned(monkeypatch):
    state = {"problems": []}

    def check(video):
        frames = [(0, b"f0"), (2, b"f1")]
        return shoe_check.Turn(b"cut", frames, [R, FR], list(state["problems"]))

    monkeypatch.setattr(shoe_check, "check_turn", check)
    return state


def test_evaluate_judges_the_labelled_frames(monkeypatch, turned):
    calls = []

    def judge(video, references, **kw):
        calls.append((video, dict(references), kw))
        return Verdict(True, description="faithful")

    monkeypatch.setattr(video_fidelity, "evaluate_video_fidelity", judge)
    verdict, video = shoe_check.evaluate(b"clip", REFS)
    assert verdict.publish and video == b"cut"
    [(judged, references, kw)] = calls
    assert judged == b"cut" and list(references) == ["right", "back_right"]
    assert kw["frames"] == [(0, b"f0"), (2, b"f1")] and kw["labels"] == [R, FR]
    assert (kw["orientation"], kw["judge"]) == ("shoe", "shoe")


def test_evaluate_rejects_a_bad_turn_without_judging(turned):
    turned["problems"] = ["turns back at 2.0 s"]
    verdict, _ = shoe_check.evaluate(b"clip", REFS)
    assert not verdict.publish and not verdict.error
    assert verdict.description == "turns back at 2.0 s"
    assert verdict.imperfection_count == shoe_check.REJECT_IMPERFECTIONS


def test_evaluate_without_judged_views_publishes_a_good_turn(turned):
    verdict, _ = shoe_check.evaluate(b"clip", {"left": b"x"})
    assert verdict.publish


def test_a_labelling_failure_is_an_eval_error(monkeypatch):
    def fail(video):
        raise GenerationError("judge down")

    monkeypatch.setattr(shoe_check, "check_turn", fail)
    verdict, video = shoe_check.evaluate(b"clip", REFS)
    assert verdict.error and not verdict.publish and video == b"clip"
