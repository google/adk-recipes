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

"""Tests for the blind-spot check: frames showing what no reference shows."""

import pytest

from genmedia4commerce.core import media, video
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval.unseen import unseen_frames

FRAMES = [png(40, 40, (i, i, i)) for i in range(4)]
REFS = [png(50, 80, (9, 9, 9)), png(50, 80, (8, 8, 8))]


@pytest.fixture(autouse=True)
def frames(monkeypatch):
    monkeypatch.setattr(video, "extract_frames", lambda clip, fps=None: FRAMES)
    monkeypatch.setattr(media, "resize_max", lambda data, side: data)


def test_one_call_with_the_references_then_the_frames(fake_gen):
    fake_gen.json({"unseen_frames": [2, 1, 2, 9, -1], "what": "the back"})
    found, what = unseen_frames(b"clip", REFS)
    assert (found, what) == ([1, 2], "the back")  # sorted, in range, unique
    (call,) = fake_gen.calls
    assert call["raw_parts"] == [
        "REFERENCE_0:",
        REFS[0],
        "REFERENCE_1:",
        REFS[1],
        "FRAME_0:",
        FRAMES[0],
        "FRAME_1:",
        FRAMES[1],
        "FRAME_2:",
        FRAMES[2],
        "FRAME_3:",
        FRAMES[3],
    ]
    assert "frames of the video (FRAME_0 to FRAME_3)" in call["system"]
    assert call["params"].model == get_models().text


@pytest.mark.parametrize(
    "raw", [{"unseen_frames": [], "what": "x"}, {}, "garbage"]
)
def test_nothing_unseen(fake_gen, raw):
    fake_gen.json(raw)
    assert unseen_frames(b"clip", REFS) == ([], "")
