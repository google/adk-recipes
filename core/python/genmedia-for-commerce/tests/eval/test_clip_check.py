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

"""Tests for the whole-clip check: the same person, nothing invented."""

import io

import pytest
from PIL import Image

from genmedia4commerce.core.errors import EvalError, GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import clip_check
from tests.core.test_video import moving_square_clip

FACE, FRONT, BACK = png(color=(1, 1, 1)), png(color=(2, 2, 2)), png()
FRAMES = [(0.0, png(color=(5, 0, 0))), (1.5, png(color=(6, 0, 0)))]
SAME = {
    "same_person": True,
    "face_reason": "same face",
    "invented_frames": [],
    "invented_what": "",
}


def test_the_call_sees_the_face_the_references_and_the_frames(fake_gen):
    fake_gen.json(SAME)
    assert clip_check.check_clip(FRAMES, FRONT, BACK, FACE) == SAME
    (call,) = fake_gen.calls
    assert call["raw_parts"] == [
        "FACE:",
        FACE,
        "REFERENCE_0 (try-on image, front):",
        FRONT,
        "REFERENCE_1 (back):",
        BACK,
        "FRAME_0 (0.0 s):",
        FRAMES[0][1],
        "FRAME_1 (1.5 s):",
        FRAMES[1][1],
    ]
    params = call["params"]
    assert params.model == get_models().judge
    assert params.thinking == "LOW"
    assert params.media_resolution == "MEDIA_RESOLUTION_MEDIUM"
    assert params.deadline_s == clip_check.DEADLINE_S == 30
    assert call["schema"] is clip_check.FACE_SCHEMA
    assert clip_check.IDENTITY in call["system"]
    assert clip_check.INVENTED in call["system"]
    assert "2 frames of the video" in call["system"]


def test_without_a_face_or_a_back_there_is_no_identity_question(fake_gen):
    fake_gen.json({"invented_frames": [], "invented_what": ""})
    found = clip_check.check_clip(FRAMES, FRONT)
    assert found == {
        "same_person": None,
        "face_reason": "",
        "invented_frames": [],
        "invented_what": "",
    }
    (call,) = fake_gen.calls
    assert call["raw_parts"][:3] == [
        "REFERENCE_0 (try-on image, front):",
        FRONT,
        "FRAME_0 (0.0 s):",
    ]
    assert call["schema"] is clip_check.SCHEMA
    assert "same_person" not in call["schema"]["properties"]
    assert clip_check.IDENTITY not in call["system"]
    assert "FACE" not in call["system"]


def test_invented_frames_are_kept_in_range_and_sorted(fake_gen):
    fake_gen.json(
        {**SAME, "invented_frames": [1, 1, 7, -1], "invented_what": "the back"}
    )
    found = clip_check.check_clip(FRAMES, FRONT, face=FACE)
    assert found["invented_frames"] == [1]
    assert found["invented_what"] == "the back"


def test_a_different_person_is_reported(fake_gen):
    fake_gen.json({**SAME, "same_person": False, "face_reason": "other face"})
    found = clip_check.check_clip(FRAMES, FRONT, face=FACE)
    assert (found["same_person"], found["face_reason"]) == (
        False,
        "other face",
    )


@pytest.mark.parametrize(
    "answer", [{"invented_frames": []}, ["not", "an", "object"]]
)
def test_an_answer_without_its_fields_raises(fake_gen, answer):
    fake_gen.json([answer])
    with pytest.raises(EvalError):
        clip_check.check_clip(FRAMES, FRONT, face=FACE)


def test_a_failed_call_raises(fake_gen):
    fake_gen.json(GenerationError("no answer within 30s"))
    with pytest.raises(GenerationError):
        clip_check.check_clip(FRAMES, FRONT, face=FACE)


def test_one_frame_per_second_the_stillest_of_each():
    # 3 s at 4 fps: the stillest frame of each second (the first frame has
    # no motion by definition).
    clip = moving_square_clip(n=12, fps=4, still={6, 11})
    frames = clip_check.pick_frames(clip)
    assert [s for s, _ in frames] == [0.0, 1.5, 2.75]
    assert all(image[:4] == b"\x89PNG" for _, image in frames)


def test_frames_are_downscaled_to_768(monkeypatch):
    big = png(1536, 1024)
    monkeypatch.setattr(clip_check.video, "probe", lambda clip: (2, 1.0, 2000))
    monkeypatch.setattr(
        clip_check.video,
        "pick_stable_frames",
        lambda clip, window_s: [(0, big), (1, big)] if window_s == 1.0 else [],
    )
    frames = clip_check.pick_frames(b"clip")
    assert [s for s, _ in frames] == [0.0, 1.0]
    assert Image.open(io.BytesIO(frames[0][1])).size == (768, 512)
