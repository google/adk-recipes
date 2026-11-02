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

"""Tests for the video identity check: one judge call, face and frames."""

import pytest

from genmedia4commerce.core.errors import EvalError, GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import identity

FACE = png(30, 30, (7, 7, 7))
FRAMES = [png(40, 40, (i, i, i)) for i in range(3)]


def test_one_judge_call_with_the_face_then_the_frames(fake_gen):
    fake_gen.json({"same_person": True, "reason": "same face"})
    assert identity.same_person(FRAMES, FACE) == (True, "same face")
    (call,) = fake_gen.calls
    assert call["raw_parts"] == [
        "FACE:",
        FACE,
        "FRAME_0:",
        FRAMES[0],
        "FRAME_1:",
        FRAMES[1],
        "FRAME_2:",
        FRAMES[2],
    ]
    assert call["system"] == identity.SYSTEM
    assert call["schema"] == identity.SCHEMA
    assert call["schema"]["required"] == ["same_person", "reason"]
    assert call["params"].model == get_models().judge


def test_another_person(fake_gen):
    fake_gen.json({"same_person": False, "reason": "a different face"})
    assert identity.same_person(FRAMES, FACE) == (False, "a different face")


@pytest.mark.parametrize(
    "raw", [{"reason": "x"}, {"same_person": "yes"}, ["garbage"]]
)
def test_an_answer_without_a_verdict_is_an_eval_error(fake_gen, raw):
    fake_gen.json([raw])  # a list is a queue: wrap the answer
    with pytest.raises(EvalError):
        identity.same_person(FRAMES, FACE)


def test_a_failed_call_raises(fake_gen):
    fake_gen.json(GenerationError("judge down"))
    with pytest.raises(GenerationError):
        identity.same_person(FRAMES, FACE)


def test_the_prompt_is_short_and_generic():
    assert len(identity.SYSTEM) < 1000
    assert "same_person false" in identity.SYSTEM
    assert "glasses may hide the eyes" in identity.SYSTEM
