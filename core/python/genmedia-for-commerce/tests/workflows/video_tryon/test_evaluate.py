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


"""Video Try-On evaluation: the identity check, each product on the still
frames and the close-ups, and the blind spots."""

import threading
import time

import pytest

from genmedia4commerce.core import video
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
    Verdict,
    quality_score,
)
from genmedia4commerce.eval import outfit as outfit_eval
from genmedia4commerce.eval import unseen
from genmedia4commerce.eval.outfit import evaluate_outfit as real_outfit
from genmedia4commerce.workflows.video_tryon import checks
from genmedia4commerce.workflows.video_tryon.evaluate import evaluate_clip
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon
from genmedia4commerce.workflows.video_tryon.refs import build_references
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    Shot,
    VideoTryOnRequest,
)
from tests.workflows.video_tryon.conftest import (
    BACK,
    CLIP,
    FACE,
    FRAMES,
    TRYON,
    product_png,
)

BAG, TOP = product_png(0), product_png(1)  # the photos sent to the video
BAG_UP, TOP_UP, TOP_BACK = product_png(5), product_png(6), product_png(7)
POSE = Plan([Shot("pose", 8.0)])


async def collect(req):
    return [event async for event in run_video_tryon(req)]


def variations(events):
    return [e for e in events if e.type == "variation"]


def request(**kw):
    return VideoTryOnRequest(tryon_image=TRYON, **kw)


def one(**kw):
    return request(policy=RunPolicy(num_variations=1), **kw)


@pytest.fixture(autouse=True)
def one_video(videos_per_image):
    """One video per run here unless a test asks for more."""
    videos_per_image(1)


# --- identity ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_sees_the_face_crop_and_the_stable_frames(
    fake_gen, fake_eval
):
    fake_gen.video(CLIP)
    (v,) = variations(await collect(one()))
    assert fake_eval.identity_calls == [(FRAMES, FACE)]
    assert v.verdict.details["identity"] == {
        "frames": [0, 1, 2],
        "same_person": True,
        "reason": "same person",
    }
    assert v.status == "ready"
    assert v.score == quality_score(True, 0) == 100.0


def test_identity_sees_up_to_six_frames_spread_over_the_clip(
    fake_eval, monkeypatch
):
    stable = [(i * 12, png(8, 8, (i, 0, 0))) for i in range(20)]
    monkeypatch.setattr(video, "pick_stable_frames", lambda clip: stable)
    verdict = judge(request())
    ((frames, face),) = fake_eval.identity_calls
    picked = [0, 4, 8, 11, 15, 19]  # the first, the last and evenly between
    assert frames == [stable[k][1] for k in picked] and face == FACE
    assert verdict.details["identity"]["frames"] == [k * 12 for k in picked]


@pytest.mark.asyncio
async def test_another_person_rejects_with_the_reason(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    fake_eval.identity = (False, "a different face from FRAME_1 on")
    (v,) = variations(await collect(one()))
    assert v.status == "rejected"
    assert v.verdict.description == "face: a different face from FRAME_1 on"
    assert v.verdict.details["identity"]["same_person"] is False
    assert "checks" not in v.verdict.details  # the judges are not waited for
    assert v.score == quality_score(False, 0) == 70.0


@pytest.mark.asyncio
async def test_a_failed_identity_check_is_skipped_and_said(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    fake_eval.identity = GenerationError("judge down")
    (v,) = variations(await collect(one()))
    assert v.status == "ready"
    assert v.verdict.description.endswith("; face check skipped (failed)")
    assert v.verdict.details["identity"] == {
        "frames": [0, 1, 2],
        "same_person": None,
        "reason": "judge down",
    }
    assert len(fake_eval.judge_calls) == 3


@pytest.mark.asyncio
async def test_stable_frames_decoded_once_per_variation(
    fake_gen, fake_eval, monkeypatch, videos_per_image
):
    videos_per_image(2)
    decoded = []

    def pick_stable_frames(clip, **_kw):
        decoded.append(clip)
        return list(enumerate(FRAMES))

    monkeypatch.setattr(video, "pick_stable_frames", pick_stable_frames)
    fake_gen.video(CLIP)
    events = await collect(request(policy=RunPolicy(num_variations=2)))
    assert [v.status for v in variations(events)] == ["ready"] * 2
    assert decoded == [CLIP, CLIP]  # the close-ups reuse identity's frames


# --- frames and views ------------------------------------------------------


def test_frames_are_the_stillest_plus_the_middle_of_a_close_shot(
    monkeypatch,
):
    """10 s plan on an 8 s clip: the bag shot is at 3.2-4.8 s, not 4-6 s."""
    monkeypatch.setattr(video, "probe", lambda clip: (192, 24.0, 8000))
    still = [(30, b"a"), (60, b"b"), (150, b"c"), (180, b"d")]
    monkeypatch.setattr(video, "stillest_frames", lambda clip: still)
    stable = [(i, b"s%d" % i) for i in range(0, 192, 12)]
    plan = Plan(
        [Shot("pose", 4.0), Shot("detail", 2.0, "bag"), Shot("pose", 4.0)]
    )
    frames = checks.pick_frames(CLIP, stable, plan)
    assert [f.index for f in frames] == [30, 60, 96, 150, 180]
    assert [f.second for f in frames] == [1.25, 2.5, 4.0, 6.25, 7.5]
    assert [f.closeup for f in frames] == [False, False, True, False, False]
    assert frames[2].image == b"s96"


def test_a_still_frame_in_the_close_shot_stands_for_its_middle(monkeypatch):
    monkeypatch.setattr(video, "probe", lambda clip: (240, 24.0, 10000))
    still = [(30, b"a"), (125, b"b"), (200, b"c")]  # 125: 5.2 s
    monkeypatch.setattr(video, "stillest_frames", lambda clip: still)
    stable = [(i, b"s") for i in range(0, 240, 12)]
    plan = Plan(
        [Shot("pose", 4.0), Shot("detail", 2.0, "bag"), Shot("pose", 4.0)]
    )
    frames = checks.pick_frames(CLIP, stable, plan)
    assert [(f.index, f.closeup) for f in frames] == [
        (30, False),
        (125, True),
        (200, False),
    ]


def test_a_close_up_frame_is_front(fake_eval):
    frames = [
        checks.Frame(0, 0.0, FRAMES[0]),
        checks.Frame(1, 2.5, FRAMES[1], closeup=True),
        checks.Frame(2, 5.0, FRAMES[2]),
    ]
    fake_eval.views = ["front", "back", "back"]  # no face in the close-up
    checks.label_views(frames, {"front": TRYON})
    assert [f.view for f in frames] == ["front", "front", "back"]
    (call,) = fake_eval.label_calls
    assert call["strategy"] == "gemini"
    assert call["references"] == {"front": TRYON}


def test_failed_view_labels_judge_every_frame_as_front(monkeypatch):
    frames = [checks.Frame(0, 0.0, FRAMES[0]), checks.Frame(1, 2.5, FRAMES[1])]

    def broken(*_a, **_kw):
        raise RuntimeError("labeller down")

    monkeypatch.setattr(checks.orientation, "label_frames", broken)
    checks.label_views(frames, {"front": TRYON})
    assert [f.view for f in frames] == ["front", "front"]


def test_a_clip_with_no_frames_is_a_retryable_error(
    fake_gen, fake_eval, monkeypatch
):
    monkeypatch.setattr(checks, "pick_frames", lambda *_a: [])
    verdict = judge(two())
    assert not verdict.publish and verdict.error
    assert "no frames decoded" in verdict.description
    assert fake_eval.judge_calls == []


# --- products, routing and visibility ---------------------------------------


def outfit(bag_refs=None, top_refs=None) -> Classification:
    """A bag (front upload) and a top (front and back uploads)."""
    bag = [ProductRef(BAG_UP, "front")] if bag_refs is None else bag_refs
    top = top_refs
    if top is None:
        top = [ProductRef(TOP_UP, "front"), ProductRef(TOP_BACK, "back")]
    products = [
        Product("A", "bag", "crossbody bag", bag),
        Product("B", "top", "wrap top", top),
    ]
    return Classification("outfit", products, "full_body")


def judge(clip_req, plan=POSE):
    """Run ``evaluate_clip`` on the fake clip for ``clip_req``."""
    refs = build_references(clip_req, face=FACE, framing=None)
    return evaluate_clip(CLIP, clip_req, FACE, plan, refs)


def two(**kw) -> VideoTryOnRequest:
    kw.setdefault("classification", outfit())
    return request(product_images=[BAG, TOP], **kw)


def calls(fake_eval):
    """(product_filter, frame number, references) of each judge call."""
    return sorted(
        (c["product_filter"], FRAMES.index(c["target"]), c["references"])
        for c in fake_eval.judge_calls
    )


def visibility(*rows):
    return {
        "frames": [{"frame": k, "visible": list(r)} for k, r in enumerate(rows)]
    }


def test_each_visible_product_is_judged_against_its_upload(fake_gen, fake_eval):
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))
    verdict = judge(two())
    assert calls(fake_eval) == [
        ("bag", 0, [BAG_UP]),
        ("bag", 2, [BAG_UP]),
        ("top", 1, [TOP_UP]),
        ("top", 2, [TOP_UP]),
    ]
    assert all(c["ignore_text_defects"] for c in fake_eval.judge_calls)
    assert verdict.publish and not verdict.error
    assert verdict.description == (
        "all 4 product checks passed (2 products x 3 frames)"
    )
    assert verdict.details["frames"] == [
        {"index": 0, "second": 0.0, "view": "front", "visible": ["bag"]},
        {"index": 1, "second": 2.5, "view": "front", "visible": ["top"]},
        {"index": 2, "second": 5.0, "view": "front", "visible": ["bag", "top"]},
    ]
    assert verdict.details["checks"][0] == {
        "product": "bag",
        "frame_index": 0,
        "second": 0.0,
        "view": "front",
        "publish": True,
        "checked": True,
        "imperfections": 0,
        "findings": [],
        "minor": [],
        "error": None,
    }


def test_garments_without_a_video_photo_are_judged_against_their_uploads(
    fake_gen, fake_eval
):
    """The video gets only the bag and shoes photos; the coat and the
    trousers are still judged, each against its own upload."""
    photos = [product_png(k) for k in range(8, 12)]  # coat, trousers, ...
    ups = [product_png(k) for k in range(12, 16)]  # their uploads
    kinds = ["full_body_outer", "bottom", "bag", "footwear"]
    look = Classification(
        "outfit",
        [
            Product(chr(65 + k), kind, "item", [ProductRef(ups[k], "front")])
            for k, kind in enumerate(kinds)
        ],
        "full_body",
    )
    req = request(product_images=photos, classification=look)
    refs = build_references(req, face=FACE, framing=None)
    assert [c.name for c in refs.closeups] == ["bag", "shoes"]
    assert refs.images[2:] == photos[2:]  # no coat or trousers photo
    fake_eval.still = [0]  # one frame, showing every product
    fake_gen.json(visibility(["coat", "bottoms", "bag", "shoes"]))
    evaluate_clip(CLIP, req, FACE, POSE, refs)
    assert calls(fake_eval) == [
        ("bag", 0, [ups[2]]),
        ("bottom", 0, [ups[1]]),
        ("footwear", 0, [ups[3]]),
        ("full_body_outer", 0, [ups[0]]),
    ]


def test_the_visibility_call(fake_gen, fake_eval):
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))
    judge(two())
    (call,) = [c for c in fake_gen.calls if c["fn"] == "generate_json"]
    assert call["params"].model == get_models().text_lite
    assert call["params"].thinking == "LOW"
    assert (
        call["raw_parts"][0] == "PRODUCTS:\nbag: crossbody bag\ntop: wrap top"
    )
    assert call["raw_parts"][1:] == [
        part for k, f in enumerate(FRAMES) for part in (f"FRAME_{k}:", f)
    ]
    shown = call["schema"]["properties"]["frames"]["items"]["properties"]
    assert shown["visible"]["items"]["enum"] == ["bag", "top"]


def test_visibility_frames_are_resized_to_512(fake_gen, fake_eval):
    big = png(1024, 2048, (9, 9, 9))
    fake_gen.json(visibility(["bag"]))
    checks.visible(
        [checks.Frame(0, 0.0, big)],
        [checks.Item("bag", "bag", "bag", [BAG], [])],
    )
    sent = fake_gen.calls[0]["raw_parts"][2]
    assert sent != big and sent[:4] == b"\x89PNG"


def test_a_failed_visibility_call_counts_every_product(fake_gen, fake_eval):
    fake_gen.json(GenerationError("lite model down"))
    verdict = judge(two())
    assert len(calls(fake_eval)) == 6  # 2 products x 3 frames
    assert all(
        f["visible"] == ["bag", "top"] for f in verdict.details["frames"]
    )


def test_a_product_no_frame_shows_is_judged_on_the_first_front_frame(
    fake_gen, fake_eval
):
    fake_eval.views = ["back", "front", "front"]
    fake_gen.json(visibility(["top"], ["top"], ["top"]))
    judge(two())
    assert calls(fake_eval) == [
        ("bag", 1, [BAG_UP]),  # missing here: the judge says so
        ("top", 0, [TOP_BACK]),
        ("top", 1, [TOP_UP]),
        ("top", 2, [TOP_UP]),
    ]


def test_back_frames_use_back_uploads_or_skip_the_product(fake_gen, fake_eval):
    fake_eval.views = ["front", "back", "left"]
    fake_gen.json(GenerationError("down"))
    verdict = judge(two())
    assert calls(fake_eval) == [
        ("bag", 0, [BAG_UP]),  # no back upload: not judged on frame 1
        ("bag", 2, [BAG_UP]),
        ("top", 0, [TOP_UP]),
        ("top", 1, [TOP_BACK]),
        ("top", 2, [TOP_UP]),
    ]
    assert [f["view"] for f in verdict.details["frames"]] == [
        "front",
        "back",
        "left",
    ]


def test_the_close_up_middle_is_judged_as_a_front_frame(fake_gen, fake_eval):
    """The bag shot is at 1.9-4.7 s of the 7.5 s clip; frame 1 (2.5 s) is
    its middle stable frame, frames 0 and 2 the stillest."""
    fake_eval.still = [0, 2]
    fake_eval.views = ["front", "back", "front"]  # the close-up has no face
    plan = Plan(
        [Shot("pose", 2.0), Shot("detail", 3.0, "bag"), Shot("pose", 3.0)]
    )
    fake_gen.json(visibility(["top"], ["bag"], ["top"]))
    verdict = judge(two(), plan)
    assert calls(fake_eval) == [
        ("bag", 1, [BAG_UP]),
        ("top", 0, [TOP_UP]),
        ("top", 2, [TOP_UP]),
    ]
    assert verdict.details["frames"][1]["view"] == "front"


def test_without_uploads_the_photo_sent_to_the_video_is_used(
    fake_gen, fake_eval
):
    # A direct animation through the API: the classification has no refs.
    # The top's photo is not sent (a garment): it is cropped as it would be.
    fake_eval.views = ["front", "back", "front"]
    fake_gen.json(GenerationError("down"))
    judge(two(classification=outfit(bag_refs=[], top_refs=[])))
    assert calls(fake_eval) == [
        ("bag", 0, [BAG]),
        ("bag", 2, [BAG]),
        ("top", 0, [TOP]),
        ("top", 2, [TOP]),
    ]


def test_unclassified_products_are_filtered_by_their_close_up_name(
    fake_gen, fake_eval
):
    fake_gen.json(GenerationError("down"))
    judge(request(product_images=[BAG, TOP]))
    assert {(c[0], c[2][0]) for c in calls(fake_eval)} == {
        ("product", BAG),
        ("product 2", TOP),
    }


def test_without_product_photos_the_outfit_is_judged_on_the_tryon(
    fake_gen, fake_eval
):
    fake_eval.views = ["front", "back", "front"]
    verdict = judge(request(back_image=BACK))
    assert calls(fake_eval) == [
        (None, 0, [TRYON]),
        (None, 1, [BACK]),
        (None, 2, [TRYON]),
    ]
    assert fake_gen.calls == []  # the outfit is always visible: no call
    assert verdict.description == (
        "all 3 product checks passed (1 products x 3 frames)"
    )


def test_nothing_to_judge_passes_on_face_and_blind_spots(fake_gen, fake_eval):
    fake_eval.views = ["back"] * 3  # a back but no back image
    verdict = judge(request())
    assert fake_eval.judge_calls == []
    assert verdict.publish and not verdict.error
    assert verdict.description == "no product photo to check"


# --- the verdict -----------------------------------------------------------


def test_a_rejected_check_names_the_product_and_the_second(fake_gen, fake_eval):
    fake_gen.json(GenerationError("down"))
    padlock = {"findings": ["padlock on the front", "two handles"]}
    fake_eval.verdicts[("bag", 1)] = Verdict(False, 2, "x", details=padlock)
    fake_eval.verdicts[("top", 2)] = Verdict(False, 1, "collar differs")
    verdict = judge(two())
    assert not verdict.publish and not verdict.error
    assert verdict.imperfection_count == 3
    assert verdict.description == (
        "rejected: bag at 2.5s: padlock on the front; top at 5s: collar differs"
    )
    failed = [c for c in verdict.details["checks"] if not c["publish"]]
    assert [(c["product"], c["findings"]) for c in failed] == [
        ("bag", ["padlock on the front", "two handles"]),
        ("top", ["collar differs"]),
    ]


def test_one_judge_call_per_frame_with_every_product_it_shows(
    fake_gen, fake_eval
):
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))
    judge(two())
    assert sorted(fake_eval.outfit_calls) == [
        (0, ["bag"]),
        (1, ["top"]),
        (2, ["bag", "top"]),
    ]


def test_a_rejected_frame_naming_no_product_rejects_all_it_shows(
    fake_gen, fake_eval, monkeypatch
):
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))

    def evaluate_outfit(products, target, **kwargs):
        if FRAMES.index(target) == 2:
            return Verdict(False, 1, "garments clip", details={"products": []})
        return Verdict(True, details={"products": []})

    monkeypatch.setattr(outfit_eval, "evaluate_outfit", evaluate_outfit)
    verdict = judge(two())
    assert not verdict.publish
    failed = [c for c in verdict.details["checks"] if not c["publish"]]
    assert [
        (c["product"], c["frame_index"], c["findings"]) for c in failed
    ] == [
        ("bag", 2, ["garments clip"]),
        ("top", 2, ["garments clip"]),
    ]


def test_a_video_frame_is_judged_without_a_wearer(
    fake_gen, fake_eval, monkeypatch
):
    """The video judge is the photo judge's, unchanged: no wearer image,
    so no body check and the same schema."""
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))
    seen = []

    def evaluate_outfit(products, target, **kwargs):
        seen.append(kwargs)
        return Verdict(True, details={"products": []})

    monkeypatch.setattr(outfit_eval, "evaluate_outfit", evaluate_outfit)
    assert judge(two()).publish
    assert seen == [{"video_frame": True}] * 3


RIB = {
    "feature": "fabric surface",
    "category": "fabric_texture",
    "kind": "differs",
    "explanation": "a vertical ribbed knit instead of a smooth jersey",
}
RED = {**RIB, "category": "fabric_color", "explanation": "red, not navy"}


@pytest.fixture
def texture_judge(fake_gen, fake_eval, monkeypatch):
    """The real outfit judge; on every frame the model rejects the first
    product it shows (the bag on frames 0 and 2, the top on frame 1) for
    the defects passed to the returned setter."""
    monkeypatch.setattr(outfit_eval, "evaluate_outfit", real_outfit)

    def answer_with(*defects):
        def respond(parts, *, schema=None, **kwargs):
            if schema is not outfit_eval.OUTFIT_SCHEMA:
                return visibility(["bag"], ["top"], ["bag", "top"])
            first = {"product": "PRODUCT_1", "name": "x", "defects": defects}
            return {
                "products": [first],
                "publish": False,
                "imperfection_count": len(defects),
                "description": "The fabric differs.",
            }

        fake_gen.json(respond)

    return answer_with


def test_a_texture_only_mismatch_lowers_the_score_but_publishes(
    texture_judge,
):
    """The live case: one product's knit differs on some frames. Each
    product counts its texture difference once, however many frames."""
    texture_judge(RIB)
    verdict = judge(two())
    assert verdict.publish and not verdict.error
    assert verdict.imperfection_count == 2  # the bag and the top, once each
    assert verdict.details["score"] == quality_score(True, 2) == 70.0
    assert verdict.description == (
        "all 4 product checks passed (2 products x 3 frames); "
        "minor differences (score only): "
        "bag at 0s: fabric_texture: fabric surface — a vertical ribbed knit "
        "instead of a smooth jersey; "
        "top at 2.5s: fabric_texture: fabric surface — a vertical ribbed "
        "knit instead of a smooth jersey; "
        "bag at 5s: fabric_texture: fabric surface — a vertical ribbed knit "
        "instead of a smooth jersey"
    )
    bag = verdict.details["checks"][0]
    assert (bag["publish"], bag["imperfections"]) == (True, 1)
    assert bag["minor"] == bag["findings"]


def test_a_colour_mismatch_next_to_a_texture_one_still_rejects(
    texture_judge,
):
    texture_judge(RIB, RED)
    verdict = judge(two())
    assert not verdict.publish and not verdict.error
    failed = [c for c in verdict.details["checks"] if not c["publish"]]
    assert [(c["product"], c["frame_index"]) for c in failed] == [
        ("bag", 0),
        ("top", 1),
        ("bag", 2),
    ]
    assert verdict.description.startswith("rejected: bag at 0s: ")


def test_a_texture_only_product_passes_in_a_rejected_frame(
    fake_gen, fake_eval, monkeypatch
):
    """Frame 2 rejects the top for its colour; the bag there has only a
    texture difference, so its check passes and adds one imperfection."""
    fake_gen.json(visibility(["bag"], ["top"], ["bag", "top"]))

    def evaluate_outfit(products, target, **kwargs):
        if FRAMES.index(target) != 2:
            return Verdict(True, details={"products": []})
        rib, red = "fabric_texture: knit", "fabric_color: red"
        found = [
            {"index": 1, "findings": [rib], "minor": [rib]},
            {"index": 2, "findings": [red], "minor": []},
        ]
        return Verdict(False, 2, "red top", details={"products": found})

    monkeypatch.setattr(outfit_eval, "evaluate_outfit", evaluate_outfit)
    verdict = judge(two())
    assert not verdict.publish
    at_2 = {
        c["product"]: c["publish"]
        for c in verdict.details["checks"]
        if c["frame_index"] == 2
    }
    assert at_2 == {"bag": True, "top": False}
    assert verdict.imperfection_count == 2  # the red top, the bag's knit
    assert verdict.description == (
        "rejected: top at 5s: fabric_color: red; "
        "minor differences (score only): bag at 5s: fabric_texture: knit"
    )


@pytest.fixture
def slow_judge(monkeypatch, fake_eval):
    """Judges of the frames in ``slow`` answer after the (shortened) cap."""
    monkeypatch.setattr(checks, "JUDGE_CAP_S", 0.2)
    slow: set[int] = set()

    def judge_fn(products, target, **kwargs):
        if FRAMES.index(target) in slow:
            time.sleep(0.6)
        return fake_eval.outfit(products, target, **kwargs)

    monkeypatch.setattr(outfit_eval, "evaluate_outfit", judge_fn)
    return slow


def test_a_judge_past_the_cap_is_not_checked(fake_gen, fake_eval, slow_judge):
    slow_judge.add(1)
    verdict = judge(request())
    assert verdict.publish and not verdict.error
    assert verdict.description == (
        "all 2 product checks passed (1 products x 2 frames); "
        "1 checks not finished in time"
    )
    late = verdict.details["checks"][1]
    assert (late["frame_index"], late["checked"], late["publish"]) == (
        1,
        False,
        None,
    )
    assert late["error"] is None


def test_no_judge_in_time_is_a_retryable_error(fake_gen, fake_eval, slow_judge):
    slow_judge.update({0, 1, 2})
    start = time.monotonic()
    verdict = judge(request())
    assert time.monotonic() - start < 0.5  # the cap, not the slow judges
    assert verdict.error and not verdict.publish
    assert verdict.description == (
        "no product check finished; 3 checks not finished in time"
    )


def test_errored_judges_are_not_rejections(fake_gen, fake_eval):
    fake_eval.verdicts[(None, 0)] = Verdict(False, error=True, description="x")
    fake_eval.verdicts[(None, 1)] = GenerationError("judge down")
    verdict = judge(request())
    assert verdict.publish and not verdict.error
    assert verdict.description.endswith("; 2 checks errored")
    errors = [c["error"] for c in verdict.details["checks"]]
    assert errors == ["x", "judge down", None]


def test_every_judge_errored_is_a_retryable_error(fake_gen, fake_eval):
    for k in range(3):
        fake_eval.verdicts[(None, k)] = Verdict(False, error=True)
    verdict = judge(request())
    assert verdict.error and not verdict.publish
    assert {c["error"] for c in verdict.details["checks"]} == {"judge error"}
    assert verdict.description == "no product check finished; 3 checks errored"


def test_blind_spots_reject_without_waiting_for_the_judges(
    fake_gen, fake_eval, monkeypatch
):
    release = threading.Event()

    def stuck(products, target, **kwargs):
        release.wait(5)
        return Verdict(True)

    monkeypatch.setattr(outfit_eval, "evaluate_outfit", stuck)
    fake_eval.unseen = [3, 4]
    start = time.monotonic()
    verdict = judge(request())
    release.set()
    assert time.monotonic() - start < 2
    assert not verdict.publish
    assert verdict.description == "shows what no reference shows: the back"
    assert verdict.details["unseen_frames"] == [3, 4]
    assert "checks" not in verdict.details


@pytest.mark.asyncio
async def test_a_skipped_blind_spot_check_is_said(
    fake_gen, fake_eval, monkeypatch
):
    def fail(clip, references):
        raise GenerationError("no answer within 120s")

    monkeypatch.setattr(unseen, "unseen_frames", fail)
    fake_gen.video(CLIP)
    (v,) = variations(await collect(one()))
    assert v.status == "ready"
    assert v.verdict.description.endswith("; blind-spot check skipped (failed)")
