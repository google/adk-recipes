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

"""Frozen eval API (spec §3.15): names, parameters and defaults.

This test stays after WP-E implements the modules; wave-2 workflows code
against exactly these signatures.
"""

import inspect
from typing import get_args

import pytest

from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import (
    garment,
    identity,
    motion,
    orientation,
    outfit,
    video_fidelity,
)

P = inspect.Parameter
POS, KW = P.POSITIONAL_OR_KEYWORD, P.KEYWORD_ONLY
NO = P.empty

# function -> [(name, kind, default)]
CONTRACT = {
    garment.evaluate_product_consistency: [
        ("references", POS, NO),
        ("target", POS, NO),
        ("product_filter", KW, None),
        ("ignore_text_defects", KW, False),
        ("parallel", KW, 8),
        ("visibility", KW, None),
    ],
    outfit.evaluate_outfit: [
        ("products", POS, NO),
        ("target", POS, NO),
        ("video_frame", KW, False),
        ("wearer", KW, None),
        ("face", KW, None),
    ],
    identity.same_person: [
        ("frames", POS, NO),
        ("reference_face", POS, NO),
    ],
    orientation.label_frames: [
        ("frames", POS, NO),
        ("strategy", KW, NO),
        ("references", KW, None),
    ],
    video_fidelity.evaluate_video_fidelity: [
        ("video", POS, NO),
        ("references", POS, NO),
        ("orientation", KW, NO),
        ("judge", KW, NO),
        ("product_filter", KW, None),
        ("frames", KW, None),
        ("labels", KW, None),
        ("any_frame", KW, False),
    ],
    motion.check_rotation: [("video", POS, NO)],
    motion.detect_glitches: [("video", POS, NO)],
}

RETURNS = {
    garment.evaluate_product_consistency: Verdict,
    outfit.evaluate_outfit: Verdict,
    video_fidelity.evaluate_video_fidelity: Verdict,
    motion.detect_glitches: Verdict,
}


@pytest.mark.parametrize("fn", list(CONTRACT), ids=lambda f: f.__qualname__)
def test_signature(fn):
    params = inspect.signature(fn).parameters.values()
    assert [(p.name, p.kind, p.default) for p in params] == CONTRACT[fn]


@pytest.mark.parametrize("fn", list(RETURNS), ids=lambda f: f.__qualname__)
def test_return_annotation(fn):
    assert inspect.signature(fn).return_annotation is RETURNS[fn]


def test_literal_vocabularies():
    assert get_args(orientation.Orientation) == ("shoe", "gemini")
    assert get_args(video_fidelity.Judge) == ("shoe", "garment", "product")
    assert get_args(motion.Rotation) == (
        "clockwise",
        "anticlockwise",
        "invalid",
        "unknown",
    )
    assert video_fidelity.Orientation is orientation.Orientation
