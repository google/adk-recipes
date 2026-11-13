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

import pytest

from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.testing import mp4_stub, png
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.eval import garment, orientation
from genmedia4commerce.eval import video_fidelity as vf

VIDEO = b"video-bytes"
# Six picked frames: (video frame index, png); colours make SSIM pick exact twins.
COLORS = [
    (250, 0, 0),
    (0, 250, 0),
    (0, 0, 250),
    (250, 250, 0),
    (0, 250, 250),
    (90, 90, 90),
]
FRAMES = [(i * 12, png(color=c)) for i, c in enumerate(COLORS)]
REFS = {
    "front": png(color=COLORS[0]),
    "right": png(color=COLORS[2]),
    "back": png(color=COLORS[4]),
}


@pytest.fixture
def scene(monkeypatch):
    """Fake the frame picker and the orientation labels."""
    seen = {}

    def install(labels, frames=FRAMES):
        monkeypatch.setattr(
            core_video, "pick_stable_frames", lambda video: frames
        )

        def label_frames(images, *, strategy, references=None):
            seen.update(
                images=list(images), strategy=strategy, references=references
            )
            return list(labels)

        monkeypatch.setattr(orientation, "label_frames", label_frames)
        return seen

    return install


def product_judge(answer=None, *, by_reference=None):
    """generate_json responder: one answer, or answers keyed by reference bytes."""
    lock, calls = threading.Lock(), []

    def respond(parts, **kwargs):
        with lock:
            calls.append({"parts": parts, **kwargs})
        out = by_reference[parts[1]] if by_reference else answer
        if isinstance(out, BaseException):
            raise out
        return out

    respond.calls = calls
    return respond


OK = {"same_product": True, "defects": []}


def test_each_view_judged_once(scene, fake_gen):
    seen = scene(["front", "left", "right", "other", "back", "other"])
    judge = product_judge(OK)
    fake_gen.json(judge)
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert verdict.publish and not verdict.error
    assert len(judge.calls) == 3
    per_view = verdict.details["per_view"]
    assert [(p["view"], p["frame_index"], p["publish"]) for p in per_view] == [
        ("front", 0, True),
        ("right", 24, True),
        ("back", 48, True),
    ]
    assert verdict.details["n_errored"] == 0
    assert seen["strategy"] == "gemini" and seen["references"] == REFS
    assert judge.calls[0]["system"] == vf.PRODUCT_JUDGE_SYSTEM
    assert judge.calls[0]["params"].model == get_models().judge


def test_given_frames_skip_the_decode(scene, fake_gen, monkeypatch):
    seen = scene(["front"] * 6)

    def decode(video):
        raise AssertionError("the clip must not be decoded again")

    monkeypatch.setattr(core_video, "pick_stable_frames", decode)
    fake_gen.json(product_judge(OK))
    verdict = vf.evaluate_video_fidelity(
        VIDEO,
        {"front": REFS["front"]},
        orientation="gemini",
        judge="product",
        frames=FRAMES,
    )
    assert verdict.publish
    assert seen["images"] == [frame for _, frame in FRAMES]
    assert verdict.details["per_view"][0]["frame_index"] == 0


def test_ssim_picks_the_closest_candidate(scene, fake_gen):
    scene(["front"] * 6)
    judge = product_judge(OK)
    fake_gen.json(judge)
    refs = {"front": png(color=COLORS[3])}
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="gemini", judge="product"
    )
    assert verdict.details["per_view"][0]["frame_index"] == 36
    assert judge.calls[0]["parts"][3] == FRAMES[3][1]


def test_missing_view_rejects(scene, fake_gen):
    scene(["front", "left", "right", "other", "left", "other"])
    judge = product_judge(OK)
    fake_gen.json(judge)
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert not verdict.publish and not verdict.error
    assert len(judge.calls) == 2
    back = verdict.details["per_view"][2]
    assert back == {
        "view": "back",
        "frame_index": None,
        "publish": False,
        "findings": ["view not found in video"],
        "error": False,
    }
    assert verdict.description.startswith("rejected: back: view not found")


def test_any_frame_judges_the_most_similar_frame_when_no_label_matches(
    scene, fake_gen
):
    # No frame labelled "front" (a shoe clip without a face): judge the
    # frame most similar to the reference instead of rejecting.
    scene(["back"] * 6)
    judge = product_judge(OK)
    fake_gen.json(judge)
    refs = {"front": png(color=COLORS[3])}
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="person", judge="product", any_frame=True
    )
    assert verdict.publish
    assert verdict.details["per_view"][0]["frame_index"] == 36
    assert judge.calls[0]["parts"][3] == FRAMES[3][1]


def test_any_frame_still_prefers_the_labelled_frames(scene, fake_gen):
    scene(["other", "other", "other", "front", "other", "front"])
    fake_gen.json(product_judge(OK))
    refs = {"front": png(color=COLORS[0])}  # identical to unlabelled frame 0
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="person", judge="product", any_frame=True
    )
    assert verdict.details["per_view"][0]["frame_index"] in (36, 60)


def test_a_three_quarter_reference_matches_either_neighbouring_view(
    scene, fake_gen
):
    # The generic labeller has no three-quarter labels.
    scene(["back", "back", "right", "back", "back", "back"])
    fake_gen.json(product_judge(OK))
    refs = {"front_right": png(color=COLORS[2])}
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="gemini", judge="product"
    )
    assert verdict.publish
    assert verdict.details["per_view"][0]["frame_index"] == 24


def test_a_three_quarter_label_wins_over_its_neighbours(scene, fake_gen):
    # The product spin's own labels have three-quarter views.
    scene(["front", "right", "right", "front_right", "back", "back"])
    fake_gen.json(product_judge(OK))
    refs = {"front_right": png(color=COLORS[0])}  # most like frame 0
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="gemini", judge="product"
    )
    assert verdict.details["per_view"][0]["frame_index"] == 36


def test_product_defects_counted(scene, fake_gen):
    scene(["front", "left", "right", "other", "back", "other"])
    answers = {
        REFS["front"]: OK,
        REFS["right"]: {
            "same_product": True,
            "defects": ["logo blurred", "extra button"],
        },
        REFS["back"]: {"same_product": False, "defects": []},
    }
    fake_gen.json(product_judge(by_reference=answers))
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert not verdict.publish
    assert verdict.imperfection_count == 3
    findings = {p["view"]: p["findings"] for p in verdict.details["per_view"]}
    assert findings["right"] == ["logo blurred", "extra button"]
    assert findings["back"] == ["different product"]


def test_all_judges_erroring_is_error(scene, fake_gen):
    scene(["front", "left", "right", "other", "back", "other"])
    fake_gen.json(product_judge(GenerationError("503")))
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert verdict.error and not verdict.publish
    assert verdict.details["n_errored"] == 3
    assert verdict.description.startswith("eval incomplete")


def test_partial_judge_error_is_not_error(scene, fake_gen):
    scene(["front", "left", "right", "other", "back", "other"])
    answers = {
        REFS["front"]: GenerationError("503"),
        REFS["right"]: OK,
        REFS["back"]: OK,
    }
    fake_gen.json(product_judge(by_reference=answers))
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert not verdict.publish and not verdict.error
    assert verdict.details["n_errored"] == 1


def test_garment_judge_uses_only_the_view_reference(scene, monkeypatch):
    scene(["front", "left", "right", "other", "back", "other"])
    calls = []
    lock = threading.Lock()

    def fake_eval(
        references,
        target,
        *,
        product_filter=None,
        ignore_text_defects=False,
        parallel=8,
    ):
        with lock:
            calls.append(
                (list(references), target, product_filter, ignore_text_defects)
            )
        return Verdict(publish=True, details={"findings": []})

    monkeypatch.setattr(garment, "evaluate_product_consistency", fake_eval)
    refs = {"front": REFS["front"], "back": REFS["back"]}
    verdict = vf.evaluate_video_fidelity(
        VIDEO,
        refs,
        orientation="person",
        judge="garment",
        product_filter="top",
    )
    assert verdict.publish
    assert sorted(c[0][0] for c in calls) == sorted(refs.values())
    assert all(len(c[0]) == 1 for c in calls)
    assert all(c[2] == "top" and c[3] is True for c in calls)


def test_garment_reject_findings(scene, monkeypatch):
    scene(["front", "left", "right", "other", "back", "other"])
    monkeypatch.setattr(
        garment,
        "evaluate_product_consistency",
        lambda *a, **k: Verdict(
            publish=False,
            imperfection_count=2,
            description="bad",
            details={"findings": ["structural: pocket — missing"]},
        ),
    )
    verdict = vf.evaluate_video_fidelity(
        VIDEO, {"front": REFS["front"]}, orientation="person", judge="garment"
    )
    assert not verdict.publish and verdict.imperfection_count == 2
    assert verdict.details["per_view"][0]["findings"] == [
        "structural: pocket — missing"
    ]


def test_shoe_tiers_widen_candidates(scene, fake_gen):
    scene(["front_right", "left", "right", "sole", "back", "back_left"])
    judge = product_judge({"same_shoe": True, "aspects": []})
    fake_gen.json(judge)
    refs = {
        "front": REFS["front"],
        "right": REFS["right"],
        "back_right": png(color=COLORS[5]),
    }
    verdict = vf.evaluate_video_fidelity(
        VIDEO, refs, orientation="shoe", judge="shoe"
    )
    assert verdict.publish
    picked = {p["view"]: p["frame_index"] for p in verdict.details["per_view"]}
    assert picked == {"front": 0, "right": 24, "back_right": 60}
    assert judge.calls[0]["system"] == vf.SHOE_JUDGE_SYSTEM


def test_shoe_judge_defects(scene, fake_gen):
    scene(["front"] * 6)
    fake_gen.json(
        product_judge(
            {
                "same_shoe": False,
                "aspects": [
                    {
                        "aspect": "branding",
                        "is_defect": True,
                        "explanation": "logo letters differ",
                    },
                    {
                        "aspect": "color",
                        "is_defect": False,
                        "explanation": "same",
                    },
                ],
            }
        )
    )
    verdict = vf.evaluate_video_fidelity(
        VIDEO, {"front": REFS["front"]}, orientation="shoe", judge="shoe"
    )
    assert not verdict.publish
    assert verdict.imperfection_count == 2
    assert verdict.details["per_view"][0]["findings"] == [
        "different shoe",
        "branding: logo letters differ",
    ]


def test_orientation_failure_is_error(monkeypatch):
    monkeypatch.setattr(core_video, "pick_stable_frames", lambda video: FRAMES)

    def fail(*a, **k):
        raise GenerationError("timeout")

    monkeypatch.setattr(orientation, "label_frames", fail)
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert verdict.error and not verdict.publish


def test_no_frames(scene):
    scene([], frames=[])
    verdict = vf.evaluate_video_fidelity(
        VIDEO, REFS, orientation="gemini", judge="product"
    )
    assert not verdict.publish
    assert verdict.details == {"per_view": [], "n_errored": 0}


def test_real_frame_picking(monkeypatch, fake_gen):
    monkeypatch.setattr(
        orientation, "label_frames", lambda images, **k: ["front"] * len(images)
    )
    fake_gen.json(product_judge(OK))
    verdict = vf.evaluate_video_fidelity(
        mp4_stub(frames=24),
        {"front": png()},
        orientation="gemini",
        judge="product",
    )
    assert verdict.publish
    assert verdict.details["per_view"][0]["frame_index"] is not None


def test_requires_references():
    with pytest.raises(InputError):
        vf.evaluate_video_fidelity(
            VIDEO, {}, orientation="gemini", judge="product"
        )
