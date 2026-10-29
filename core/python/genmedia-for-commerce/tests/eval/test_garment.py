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

import threading
import time

import pytest

from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.testing import png
from genmedia4commerce.eval import garment_prompts as gp
from genmedia4commerce.eval.garment import evaluate_product_consistency

TARGET = png(color=(1, 2, 3))
REFS = [png(color=(10, 10, 10)), png(color=(20, 20, 20))]


def anchor(**kw):
    return {"label": "jacket", "kind": "match", "is_defect": False, **kw}


def defect(
    label="pocket", category="structural", box=(100, 100, 600, 600), **kw
):
    return {
        "label": label,
        "category": category,
        "kind": "missing_in_target",
        "is_defect": True,
        "explanation": f"{label} is wrong",
        "out_of_scope": False,
        "ref_bbox": list(box),
        "target_bbox": list(box),
        **kw,
    }


def detector_raw(*features, present=True):
    return {
        "product_present": present,
        "silhouette": anchor(),
        "fabric_color": anchor(),
        "features": list(features),
    }


VERDICT_OK = {
    "publish": True,
    "imperfection_count": 0,
    "description": "looks right",
}


class Router:
    """Answers detector / verifier / aggregator calls by system prompt."""

    def __init__(self, detector=None, verifier=None, verdict=None):
        self.detector = detector if detector is not None else detector_raw()
        self.verifier = verifier or {"reasoning": "real", "confirm": True}
        self.verdict = verdict or VERDICT_OK
        self.calls = {"detector": [], "verifier": [], "aggregator": []}
        self.lock = threading.Lock()

    def __call__(self, parts, *, system=None, schema=None, params=None):
        if system == gp.DETECTOR_SYSTEM:
            kind, answer = "detector", self.detector
        elif system == gp.VERIFIER_SYSTEM:
            kind, answer = "verifier", self.verifier
        else:
            assert system.startswith(gp.AGGREGATE_SYSTEM)
            kind, answer = "aggregator", self.verdict
        with self.lock:
            self.calls[kind].append({"parts": parts, "system": system})
        if callable(answer):
            return answer(parts)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def test_publish_with_one_aggregator_call(fake_gen):
    router = Router()
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS, TARGET)
    assert verdict.publish and not verdict.error
    assert verdict.imperfection_count == 0
    assert verdict.description == "looks right"
    assert len(router.calls["detector"]) == 2
    assert len(router.calls["aggregator"]) == 1
    parts = router.calls["aggregator"][0]["parts"]
    assert parts[0] == "TARGET (generated) image:"
    assert (
        "REFERENCE input #0 (—):" in parts
        and "REFERENCE input #1 (—):" in parts
    )
    assert "== Input #0 (—) — 2 features matched, 0 defects ==" in parts[-1]
    per_ref = verdict.details["per_reference"]
    assert [r["index"] for r in per_ref] == [0, 1]
    assert all(r["publish"] for r in per_ref)
    assert verdict.details["findings"] == []


def test_reject_carries_defects(fake_gen):
    router = Router(
        detector=detector_raw(defect()),
        verdict={
            "publish": False,
            "imperfection_count": 2,
            "description": "bad",
        },
    )
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS, TARGET)
    assert not verdict.publish and verdict.imperfection_count == 2
    stanza = router.calls["aggregator"][0]["parts"][-1]
    assert "[1] structural / MISSING / defect" in stanza
    assert "target_size: 500 x 500 px (long_side: 500)" in stanza
    assert (
        verdict.details["findings"][0] == "structural: pocket — pocket is wrong"
    )
    assert (
        verdict.details["per_reference"][0]["defects"][0]["label"] == "pocket"
    )
    assert not verdict.details["per_reference"][0]["publish"]


def test_verifier_drop_removes_defect_from_aggregation(fake_gen):
    router = Router(
        detector=detector_raw(defect()),
        verifier={"reasoning": "occluded", "confirm": False},
    )
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS[:1], TARGET)
    assert len(router.calls["verifier"]) == 1
    assert "0 defects" in router.calls["aggregator"][0]["parts"][-1]
    assert verdict.details["findings"] == []


def test_ignore_text_defects_is_video_frame_mode(fake_gen):
    small = defect(
        "small sleeve logo", "identity_text", box=(100, 100, 200, 200)
    )
    large = defect("chest wordmark", "identity_text", box=(100, 100, 500, 700))
    router = Router(detector=detector_raw(small, large))
    fake_gen.json(router)
    verdict = evaluate_product_consistency(
        REFS[:1], TARGET, ignore_text_defects=True
    )
    detector_parts = router.calls["detector"][0]["parts"]
    assert gp.DETECTOR_VIDEO_FRAME in detector_parts
    agg = router.calls["aggregator"][0]
    assert (
        agg["system"] == gp.AGGREGATE_SYSTEM + gp.AGGREGATE_VIDEO_TEXT_TOLERANCE
    )
    assert "small sleeve logo" not in agg["parts"][-1]
    assert "chest wordmark" in agg["parts"][-1]
    assert [
        d["label"] for d in verdict.details["per_reference"][0]["defects"]
    ] == ["chest wordmark"]


def test_without_video_mode_small_text_still_counts(fake_gen):
    small = defect(
        "small sleeve logo", "identity_text", box=(100, 100, 200, 200)
    )
    router = Router(detector=detector_raw(small))
    fake_gen.json(router)
    evaluate_product_consistency(REFS[:1], TARGET)
    agg = router.calls["aggregator"][0]
    assert agg["system"] == gp.AGGREGATE_SYSTEM
    assert "small sleeve logo" in agg["parts"][-1]
    assert gp.DETECTOR_VIDEO_FRAME not in router.calls["detector"][0]["parts"]


def test_all_references_errored_is_transient_error(fake_gen):
    fake_gen.json(Router(detector=GenerationError("connection reset")))
    verdict = evaluate_product_consistency(REFS, TARGET)
    assert verdict.error and not verdict.publish
    assert verdict.description.startswith("eval incomplete")
    assert "all 2 reference(s) errored" in verdict.description
    assert [r["error"] for r in verdict.details["per_reference"]] == [
        "connection reset",
        "connection reset",
    ]


def test_partial_error_aggregates_the_rest(fake_gen):
    answers = iter([GenerationError("boom"), detector_raw()])
    lock = threading.Lock()

    def detector(parts):
        with lock:
            answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    router = Router(detector=detector)
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS, TARGET, parallel=1)
    assert verdict.publish and not verdict.error
    assert (
        "REFERENCE input #1 (—):" not in router.calls["aggregator"][0]["parts"]
    )
    assert verdict.details["per_reference"][0]["error"] == "boom"
    assert len(verdict.details["per_reference"]) == 2


def test_parallel_is_respected(fake_gen):
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def detector(parts):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return detector_raw()

    fake_gen.json(Router(detector=detector))
    refs = [png(color=(i, i, i)) for i in range(5)]
    verdict = evaluate_product_consistency(refs, TARGET, parallel=2)
    assert state["peak"] == 2
    assert len(verdict.details["per_reference"]) == 5


@pytest.mark.parametrize(
    ("product_filter", "expected"),
    [
        ("eyewear", "the glasses frame, lenses and temples"),
        ("jewelry_head", "the earrings only"),
        ("navy denim jacket", "navy denim jacket"),
    ],
)
def test_category_filter_maps_to_hint(fake_gen, product_filter, expected):
    router = Router()
    fake_gen.json(router)
    evaluate_product_consistency(
        REFS[:1], TARGET, product_filter=product_filter
    )
    focus = gp.DETECTOR_FOCUS.format(product=expected)
    assert focus in router.calls["detector"][0]["parts"]
    assert (
        f"REFERENCE input #0 ({expected}):"
        in router.calls["aggregator"][0]["parts"]
    )


def test_filtered_product_absent_everywhere(fake_gen):
    router = Router(detector=detector_raw(defect(), present=False))
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS, TARGET, product_filter="hat")
    assert not verdict.publish and not verdict.error
    assert verdict.description.startswith("No inputs were evaluated")
    assert router.calls["verifier"] == router.calls["aggregator"] == []
    assert all(r["skipped"] for r in verdict.details["per_reference"])


def test_filtered_product_absent_from_one_reference(fake_gen):
    answers = iter([detector_raw(present=False), detector_raw()])
    router = Router(detector=lambda _parts: next(answers))
    fake_gen.json(router)
    verdict = evaluate_product_consistency(
        REFS, TARGET, product_filter="hat", parallel=1
    )
    assert verdict.publish
    assert [r["skipped"] for r in verdict.details["per_reference"]] == [
        True,
        False,
    ]
    assert len(router.calls["aggregator"]) == 1


def test_absent_flag_is_ignored_without_a_product_filter(fake_gen):
    fake_gen.json(Router(detector=detector_raw(present=False)))
    verdict = evaluate_product_consistency(REFS[:1], TARGET)
    assert verdict.publish
    assert not verdict.details["per_reference"][0]["skipped"]


def test_aggregator_failure_is_error(fake_gen):
    fake_gen.json(Router(verdict=GenerationError("unparsable")))
    verdict = evaluate_product_consistency(REFS[:1], TARGET)
    assert verdict.error and not verdict.publish
    assert verdict.description.startswith("eval incomplete")


def test_no_references_is_input_error(fake_gen):
    with pytest.raises(InputError):
        evaluate_product_consistency([], TARGET)


def test_visibility_reaches_detector_and_aggregator(fake_gen):
    router = Router()
    fake_gen.json(router)
    note = "sleeves visible emerging from armholes"
    evaluate_product_consistency(
        REFS[:1], TARGET, product_filter="top", visibility=note
    )
    detector_parts = router.calls["detector"][0]["parts"]
    assert gp.DETECTOR_LAYERING.format(visibility=note) in detector_parts
    agg = router.calls["aggregator"][0]
    assert agg["system"] == gp.AGGREGATE_SYSTEM + gp.AGGREGATE_LAYERING.format(
        visibility=note
    )


@pytest.mark.parametrize("kw", [{}, {"visibility": None}])
def test_no_visibility_keeps_the_prompts_identical(fake_gen, kw):
    router = Router()
    fake_gen.json(router)
    evaluate_product_consistency(REFS[:1], TARGET, product_filter="top", **kw)
    focus = gp.DETECTOR_FOCUS.format(product=gp.PRODUCT_FILTER_HINTS["top"])
    assert router.calls["detector"][0]["parts"][4:] == [
        focus,
        gp.DETECTOR_CLOSING,
    ]
    assert router.calls["aggregator"][0]["system"] == gp.AGGREGATE_SYSTEM


def extra(label="strap padlock", category="hardware", **kw):
    return defect(label, category, kind="extra_in_target", **kw)


CATEGORIES = ["hardware", "structural", "embellishment", "identity_text"]


@pytest.mark.parametrize("category", [*CATEGORIES, "other"])
def test_fabricated_element_never_publishes(fake_gen, category):
    fake_gen.json(Router(detector=detector_raw(extra(category=category))))
    verdict = evaluate_product_consistency(
        REFS[:1], TARGET, product_filter="bag", ignore_text_defects=True
    )
    assert not verdict.publish and not verdict.error
    assert verdict.imperfection_count == 1
    assert verdict.description == (
        "REJECT: fabricated element: strap padlock. looks right"
    )


@pytest.mark.parametrize(("agg_count", "expected"), [(1, 2), (5, 5)])
def test_fabricated_elements_count(fake_gen, agg_count, expected):
    router = Router(
        detector=detector_raw(extra(), extra("tab", "structural"), defect()),
        verdict={
            "publish": False,
            "imperfection_count": agg_count,
            "description": "bad",
        },
    )
    fake_gen.json(router)
    verdict = evaluate_product_consistency(REFS[:1], TARGET)
    assert not verdict.publish and verdict.imperfection_count == expected
    assert verdict.description == (
        "REJECT: fabricated element: strap padlock; tab. bad"
    )


@pytest.mark.parametrize(
    ("finding", "confirm"),
    [
        (extra(occluded=True), True),
        (extra(out_of_scope=True), True),
        (extra(), False),  # dropped by the verifier
        (defect(), True),  # missing, not fabricated
        # small text in a video frame stays tolerated
        (extra("logo", "identity_text", box=(100, 100, 200, 200)), True),
    ],
)
def test_no_surviving_fabrication_keeps_verdict(fake_gen, finding, confirm):
    fake_gen.json(
        Router(
            detector=detector_raw(finding),
            verifier={"reasoning": "checked", "confirm": confirm},
        )
    )
    verdict = evaluate_product_consistency(
        REFS[:1], TARGET, ignore_text_defects=True
    )
    assert verdict.publish and verdict.description == "looks right"
