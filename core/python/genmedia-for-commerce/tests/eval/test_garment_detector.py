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

import io

import pytest
from PIL import Image

from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import garment_detector as gd
from genmedia4commerce.eval import garment_prompts as gp

REF, TGT = png(200, 100), png(100, 200, color=(0, 0, 255))


def anchor(**kw):
    return {"label": "jacket", "kind": "match", "is_defect": False, **kw}


def feature(**kw):
    base = {
        "label": "left chest logo",
        "category": "identity_text",
        "kind": "match",
        "is_defect": False,
        "explanation": "same logo",
        "out_of_scope": False,
    }
    return {**base, **kw}


def detector_raw(*features, present=True, **anchors):
    return {
        "product_present": present,
        "silhouette": anchors.get("silhouette", anchor()),
        "fabric_color": anchors.get("fabric_color", anchor()),
        "features": list(features),
    }


def finding(**kw):
    base = {
        "label": "pocket",
        "level": "detail",
        "category": "structural",
        "kind": "missing",
        "is_defect": True,
        "explanation": "pocket missing",
        "ref_box": [100, 100, 300, 300],
        "target_box": [100, 100, 300, 300],
    }
    return gd.Finding(**{**base, **kw})


class TestDetect:
    def test_parses_anchors_and_features(self, fake_gen):
        fake_gen.json(
            detector_raw(
                feature(),
                feature(
                    label="pocket",
                    category="structural",
                    kind="missing_in_target",
                    is_defect=True,
                ),
                feature(label="zip", category="weird", kind="extra_in_target"),
                silhouette=anchor(is_defect=True, ref_bbox=[0, 0, 1000, 1000]),
            )
        )
        result = gd.detect(REF, TGT, product_filter=None)
        cats = [(f.level, f.category, f.kind) for f in result.findings]
        assert cats == [
            ("garment", "silhouette", "pair"),
            ("garment", "fabric_color", "pair"),
            ("detail", "identity_text", "pair"),
            ("detail", "structural", "missing"),
            ("detail", "other", "extra"),
        ]
        assert result.findings[0].ref_box == [0, 0, 1000, 1000]
        assert [f.label for f in result.defects] == ["jacket", "pocket"]
        assert result.matches == 3

    def test_out_of_scope_is_never_a_defect(self, fake_gen):
        fake_gen.json(
            detector_raw(
                feature(
                    kind="extra_in_target", is_defect=True, out_of_scope=True
                )
            )
        )
        result = gd.detect(REF, TGT, product_filter=None)
        extra = result.findings[-1]
        assert extra.out_of_scope and not extra.is_defect
        assert result.defects == []

    def test_occluded_does_not_count(self, fake_gen):
        fake_gen.json(detector_raw(feature(is_defect=True, occluded=True)))
        assert gd.detect(REF, TGT, product_filter=None).defects == []

    def test_request_shape_and_judge_model(self, fake_gen):
        fake_gen.json(detector_raw())
        gd.detect(REF, TGT, product_filter="blue jeans", video_frame=True)
        call = fake_gen.calls[-1]
        assert call["system"] == gp.DETECTOR_SYSTEM
        assert call["schema"] == gp.DETECTOR_SCHEMA
        assert call["params"].model == get_models().judge
        assert call["params"].thinking == "LOW"
        raw = call["raw_parts"]
        assert raw[:4] == ["REFERENCE image:", REF, "TARGET image:", TGT]
        assert raw[4] == gp.DETECTOR_FOCUS.format(product="blue jeans")
        assert raw[5] == gp.DETECTOR_VIDEO_FRAME
        assert raw[6] == gp.DETECTOR_CLOSING

    def test_reviewer_feedback_is_a_hint_block(self, fake_gen):
        fake_gen.json(detector_raw())
        gd.detect(REF, TGT, product_filter=None, user_feedback=" logo off ")
        raw = fake_gen.calls[-1]["raw_parts"]
        assert raw[4] == gp.DETECTOR_FEEDBACK.format(feedback="logo off")
        assert "PRIOR REVIEWER FEEDBACK" in raw[4] and "> logo off\n" in raw[4]
        assert raw[5] == gp.DETECTOR_CLOSING

    def test_video_frame_and_feedback_share_one_block(self, fake_gen):
        fake_gen.json(detector_raw())
        gd.detect(
            REF, TGT, product_filter=None, video_frame=True, user_feedback="zip"
        )
        raw = fake_gen.calls[-1]["raw_parts"]
        notes = gp.VIDEO_FRAME_NOTE + "\n> zip"
        assert raw[4] == gp.DETECTOR_FEEDBACK.format(feedback=notes)
        assert raw[5] == gp.DETECTOR_CLOSING

    @pytest.mark.parametrize("feedback", [None, "", "   "])
    def test_no_feedback_adds_nothing(self, fake_gen, feedback):
        fake_gen.json(detector_raw())
        gd.detect(REF, TGT, product_filter=None, user_feedback=feedback)
        assert fake_gen.calls[-1]["raw_parts"][4:] == [gp.DETECTOR_CLOSING]

    def test_visibility_is_a_layering_fact_after_the_focus(self, fake_gen):
        fake_gen.json(detector_raw())
        note = "only the collar visible at the neckline"
        gd.detect(REF, TGT, product_filter="the shirt", visibility=note)
        raw = fake_gen.calls[-1]["raw_parts"]
        assert raw[4] == gp.DETECTOR_FOCUS.format(product="the shirt")
        assert raw[5] == gp.DETECTOR_LAYERING.format(visibility=note)
        assert raw[6] == gp.DETECTOR_CLOSING
        assert f'"{note}"' in raw[5] and "PRIOR REVIEWER" not in raw[5]

    @pytest.mark.parametrize("visibility", [None, ""])
    def test_no_visibility_adds_nothing(self, fake_gen, visibility):
        fake_gen.json(detector_raw())
        gd.detect(REF, TGT, product_filter=None, visibility=visibility)
        assert fake_gen.calls[-1]["raw_parts"][4:] == [gp.DETECTOR_CLOSING]

    def test_no_filter_no_focus(self, fake_gen):
        fake_gen.json(detector_raw())
        gd.detect(REF, TGT, product_filter=None)
        assert fake_gen.calls[-1]["raw_parts"][4:] == [gp.DETECTOR_CLOSING]

    @pytest.mark.parametrize("present", [True, False])
    def test_product_present_flag(self, fake_gen, present):
        fake_gen.json(detector_raw(present=present))
        result = gd.detect(REF, TGT, product_filter="hat")
        assert result.product_present is present

    def test_non_object_answer_is_empty(self, fake_gen):
        fake_gen.json([[]])  # the queue form: one responder returning []
        assert gd.detect(REF, TGT, product_filter="x").findings == []


class TestCrop:
    def size(self, data):
        return Image.open(io.BytesIO(data)).size

    def test_normalised_box_with_padding(self):
        img = png(1000, 1000)
        crop = gd._crop(img, (1000, 1000), [400, 400, 600, 600])
        # box 200 px, padding max(40, 60) = 60 per side
        assert self.size(crop) == (320, 320)

    def test_swapped_coords_and_min_size(self):
        img = png(1000, 500)
        crop = gd._crop(img, (1000, 500), [510, 510, 500, 500])
        w, h = self.size(crop)
        assert w >= 150 and h >= 75

    def test_clamped_at_edges(self):
        img = png(100, 100)
        crop = gd._crop(img, (100, 100), [0, 0, 1000, 1000])
        assert self.size(crop) == (100, 100)


class TestVerify:
    def test_drops_flag_when_not_confirmed(self, fake_gen):
        fake_gen.json({"reasoning": "hidden by the arm", "confirm": False})
        f = finding()
        result = gd.verify(REF, TGT, gd.DetectorResult([f]))
        assert not f.is_defect
        assert f.explanation.endswith("[verifier dropped: hidden by the arm]")
        assert result.defects == []
        call = fake_gen.calls[-1]
        assert call["system"] == gp.VERIFIER_SYSTEM
        assert call["params"].model == get_models().text_lite
        assert (
            "REF crop (zoomed view of the feature area):" in call["raw_parts"]
        )
        assert call["raw_parts"][-1].startswith(
            "Feature label: pocket\nDetector kind: missing"
        )

    def test_keeps_confirmed_flag(self, fake_gen):
        fake_gen.json({"reasoning": "really missing", "confirm": True})
        f = finding()
        gd.verify(REF, TGT, gd.DetectorResult([f]))
        assert f.is_defect

    def test_only_counting_defects_are_checked(self, fake_gen):
        fake_gen.json({"reasoning": "", "confirm": False})
        findings = [
            finding(is_defect=False),
            finding(occluded=True),
            finding(out_of_scope=True, is_defect=False),
            finding(ref_box=None, target_box=None),
        ]
        gd.verify(REF, TGT, gd.DetectorResult(findings))
        assert fake_gen.calls == []
        assert findings[3].is_defect

    def test_missing_box_says_not_available(self, fake_gen):
        fake_gen.json({"reasoning": "", "confirm": True})
        gd.verify(
            REF, TGT, gd.DetectorResult([finding(kind="extra", ref_box=None)])
        )
        assert (
            "REF crop: not available (extra-in-target case)."
            in fake_gen.calls[-1]["raw_parts"]
        )

    def test_call_failure_keeps_flag(self, fake_gen):
        fake_gen.json(GenerationError("boom"))
        f = finding()
        gd.verify(REF, TGT, gd.DetectorResult([f]))
        assert f.is_defect
