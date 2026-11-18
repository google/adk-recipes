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

"""Tests for the non-shoe spin checks (``product_check``)."""

import threading

import pytest

from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import motion, video_fidelity
from genmedia4commerce.workflows.spin import product_check
from tests.workflows.spin.conftest import clip

F, FR, R, BR, B, BL, L, FL = product_check.PRODUCT_VIEWS
TIMES = [k / 2 for k in range(17)]  # a sample every half second over 8 s


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        # round to the back, back at the front at 7 s
        (
            [F, F, FR, R, R, BR, B, B, B, BL, L, L, FL, FL, F, F, FR],
            ("closed", 14),
        ),
        # started front-left, ended front-right: a quarter turn short
        (
            [FL, FL, L, L, BL, B, B, BR, BR, R, R, FR, FR, FR, FR, FR, FR],
            ("open", None),
        ),
        # ends one view short of its start
        (
            [F, FR, R, R, BR, B, B, BL, L, L, L, FL, FL, FL, FL, FL, FL],
            ("short", None),
        ),
        # back at its start without ever reaching the far side (turned back)
        ([F, FR, R, R, FR, F, F, F, F, F, F, F, F, F, F, F, F], ("open", None)),
        # no first view to go by
        (
            ["unclear", F, FR, R, B, L, F, F, F, F, F, F, F, F, F, F, F],
            ("unknown", None),
        ),
    ],
)
def test_read_close(labels, expected):
    assert product_check.read_close(labels, TIMES) == expected


def test_the_first_view_counts_only_in_the_last_third():
    # front again at 2 s (a label slip) is not the return
    labels = [F, FR, F, R, BR, B, B, BL, L, L, L, FL, FL, FL, FL, FL, FL]
    assert product_check.read_close(labels, TIMES) == ("short", None)


# 8 s at 4 fps; frame 29 looks like frame 0 (the return), the rest differ.
PATTERNS = [0 if i in (0, 29) else 100 + i for i in range(32)]
SAMPLES = [*range(0, 32, 2), 31]  # what the labeller sees
CLOSING = [F, F, FR, R, R, BR, B, B, B, BL, L, L, FL, FL, F, F, FR]


@pytest.fixture
def checks(monkeypatch):
    """Faked flow, glitch call, labeller and judge, recorded."""
    state = {
        "rotation": "clockwise",
        "labels": CLOSING,
        "glitch": Verdict(True),
    }
    state.update(judged=[], glitched=[], labelled=[])

    def labels(frames, refs):
        state["labelled"].append(len(frames))
        if isinstance(state["labels"], Exception):
            raise state["labels"]
        return list(state["labels"])

    def glitches(video):
        state["glitched"].append(video)
        return state["glitch"]

    def judge(video, references, **kw):
        state["judged"].append({"video": video, **kw})
        return Verdict(True, description="faithful")

    monkeypatch.setattr(product_check, "label_frames", labels)
    monkeypatch.setattr(motion, "detect_glitches", glitches)
    monkeypatch.setattr(
        motion, "check_rotation", lambda video: state["rotation"]
    )
    monkeypatch.setattr(video_fidelity, "evaluate_video_fidelity", judge)
    return state


REFS = {"front": b"FRONT", "back": b"BACK"}


def test_a_closing_spin_is_cut_where_it_closes_and_judged_on_its_labels(checks):
    spin = clip(PATTERNS, fps=4)
    verdict, out = product_check.evaluate(spin, REFS)
    assert verdict.publish
    assert checks["labelled"] == [len(SAMPLES)]
    assert core_video.probe(out)[0] == 30  # ends on the frame like the first
    assert verdict.details["loop"] == {"cut_frame": 30}
    assert verdict.details["turn"]["closes"] == "closed"
    [judged] = checks["judged"]
    assert [i for i, _ in judged["frames"]] == [i for i in SAMPLES if i < 30]
    assert judged["labels"] == CLOSING[:15]
    assert checks["glitched"] == [spin]  # the clip as made


def test_a_spin_that_does_not_come_back_is_rejected_unjudged(checks):
    checks["labels"] = [
        FL,
        FL,
        L,
        L,
        BL,
        B,
        B,
        BR,
        BR,
        R,
        R,
        FR,
        FR,
        FR,
        FR,
        FR,
        FR,
    ]
    spin = clip(PATTERNS, fps=4)
    verdict, out = product_check.evaluate(spin, REFS)
    assert not verdict.publish and verdict.imperfection_count == 2
    assert "does not come back to its first view" in verdict.description
    assert "started at front_left, ended at front_right" in verdict.description
    assert out is spin and checks["judged"] == []


def test_failed_labels_fall_back_to_the_similarity_cut(checks):
    checks["labels"] = RuntimeError("judge model down")
    verdict, _ = product_check.evaluate(clip(PATTERNS, fps=4), REFS)
    assert verdict.publish
    [judged] = checks["judged"]
    assert "labels" not in judged  # the judge labels the frames itself


def test_an_anticlockwise_spin_is_reversed_with_its_labels(checks):
    checks["rotation"] = "anticlockwise"
    checks["labels"] = CLOSING[::-1]
    spin = clip(PATTERNS[::-1], fps=4)  # the closing spin played backwards
    verdict, _ = product_check.evaluate(spin, REFS)
    assert verdict.details["rotation"]["reversed"]
    assert verdict.details["turn"]["closes"] == "closed"
    assert verdict.details["loop"] == {"cut_frame": 30}


def test_flow_labels_and_glitch_call_start_together(checks, monkeypatch):
    started = {"labels": threading.Event(), "glitch": threading.Event()}
    labeller, glitcher = product_check.label_frames, motion.detect_glitches

    def labels(frames, refs):
        started["labels"].set()
        return labeller(frames, refs)

    def glitches(video):
        started["glitch"].set()
        return glitcher(video)

    def rotation(video):  # flow waits until both calls are under way
        assert all(e.wait(timeout=5) for e in started.values())
        return "clockwise"

    monkeypatch.setattr(product_check, "label_frames", labels)
    monkeypatch.setattr(motion, "detect_glitches", glitches)
    monkeypatch.setattr(motion, "check_rotation", rotation)
    verdict, _ = product_check.evaluate(clip(PATTERNS, fps=4), REFS)
    assert verdict.publish


def test_a_glitch_wins_over_a_passing_judge(checks):
    checks["glitch"] = Verdict(False, description="a plate appears at 7 s")
    verdict, _ = product_check.evaluate(clip(PATTERNS, fps=4), REFS)
    assert not verdict.publish
    assert verdict.description == "a plate appears at 7 s"
    assert verdict.details["glitch"] == "a plate appears at 7 s"
