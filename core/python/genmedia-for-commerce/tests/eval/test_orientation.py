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

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import orientation
from genmedia4commerce.eval.orientation import label_frames


def frames(n):
    return [png(color=(i, i, i)) for i in range(n)]


def test_gemini_request_layout(fake_gen):
    fake_gen.json({"labels": ["front", "right", "back"]})
    refs = {"front": png(color=(50, 0, 0)), "back": png(color=(0, 50, 0))}
    images = frames(3)
    assert label_frames(images, strategy="gemini", references=refs) == [
        "front",
        "right",
        "back",
    ]
    call = fake_gen.calls[-1]
    texts = [p for p in call["raw_parts"] if isinstance(p, str)]
    assert texts == [
        "REFERENCE front:",
        "REFERENCE back:",
        "FRAME 0:",
        "FRAME 1:",
        "FRAME 2:",
    ]
    assert call["system"].startswith(
        "You see 3 frames sampled from a 360° product video"
    )
    assert "2 reference photos" in call["system"]
    assert call["schema"] == orientation.GEMINI_SCHEMA


def test_gemini_short_answer_is_padded_with_other(fake_gen):
    fake_gen.json({"labels": ["front"]})
    assert label_frames(frames(3), strategy="gemini") == [
        "front",
        "other",
        "other",
    ]


def test_gemini_long_or_unknown_labels_are_cleaned(fake_gen):
    fake_gen.json({"labels": ["FRONT", "top", "left", "back"]})
    assert label_frames(frames(3), strategy="gemini") == [
        "front",
        "other",
        "left",
    ]


def test_gemini_non_object_answer(fake_gen):
    fake_gen.json([["front"]])
    assert label_frames(frames(2), strategy="gemini") == ["other", "other"]


def test_no_frames():
    assert label_frames([], strategy="gemini") == []


@pytest.mark.parametrize("strategy", ["sideways", "shoe", "person"])
def test_unknown_strategy(strategy):
    # Shoe frames are labelled by the shoe spin itself (shoe_check); the
    # face-yaw "person" strategy is gone (Gemini labels try-on frames).
    with pytest.raises(InputError):
        label_frames(frames(1), strategy=strategy)
