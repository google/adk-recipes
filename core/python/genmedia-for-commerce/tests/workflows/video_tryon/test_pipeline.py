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

import dataclasses

import pytest

from genmedia4commerce.core.errors import (
    SERVER_FAILED,
    GenerationError,
    InputError,
)
from genmedia4commerce.core.events import (
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import VideoParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.types import (
    Classification,
    Product,
    Verdict,
    quality_score,
)
from genmedia4commerce.eval import unseen
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon
from genmedia4commerce.workflows.video_tryon.prompt import render
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
    classification,
    product_png,
)


async def collect(req):
    return [event async for event in run_video_tryon(req)]


def variations(events):
    return [e for e in events if isinstance(e, VariationEvent)]


def request(**kw):
    return VideoTryOnRequest(tryon_image=TRYON, **kw)


def one(**kw):
    return request(policy=RunPolicy(num_variations=1), **kw)


@pytest.fixture(autouse=True)
def one_video(videos_per_image):
    """One video per run here unless a test asks for more."""
    videos_per_image(1)


TURN = Plan([Shot("pose", 4.0), Shot("turn_back", 5.0, transition="cut")])


@pytest.mark.asyncio
async def test_plan_event_then_the_videos_then_complete(
    fake_gen, fake_eval, videos_per_image
):
    videos_per_image(3)
    fake_gen.video(CLIP)
    events = await collect(request())
    assert isinstance(events[0], ProgressEvent)
    assert events[0].stage == "plan"
    assert events[0].message == "Animation: pose, 8 s"
    assert events[0].data == {
        "shots": [dataclasses.asdict(Shot("pose", 8.0))],
        "note": "",
        "references": 2,  # try-on + face
        "videos": 3,
    }
    found = variations(events)
    assert len(found) == 3
    assert all(v.status == "ready" and v.media == CLIP for v in found)
    assert isinstance(events[-1], CompleteEvent)
    assert events[-1].best_index == events[-1].ranked_indices[0]
    assert len([c for c in fake_gen.calls if c["fn"] == "generate_video"]) == 3


@pytest.mark.asyncio
async def test_the_note_of_an_adapted_plan_reaches_the_user(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    planner.plan = Plan([Shot("pose", 8.0)], note="No back photo, so...")
    events = await collect(one(motion="custom", prompt="show the back"))
    assert events[0].message == "Animation: pose, 8 s. No back photo, so..."
    assert planner.calls[0][1] == "show the back"


@pytest.mark.asyncio
async def test_auto_sends_no_request_to_the_planner(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    await collect(one(prompt="ignored outside custom"))
    assert planner.calls[0][1] is None


@pytest.mark.asyncio
async def test_the_prompt_and_the_length_come_from_the_plan(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    planner.plan = Plan(
        [
            Shot("walk", 4.0),
            Shot("detail", 3.0, region="bag"),
            Shot("pull_out", 3.0),
        ]
    )
    req = one(
        product_images=[product_png(1)],
        classification=classification("full_body", "bag"),
    )
    await collect(req)
    (call,) = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    refs = planner.calls[0][0]
    assert call["prompt"] == render(planner.plan, refs)
    assert call["prompt"].startswith("[duration=10s]")
    assert call["params"] == VideoParams(aspect_ratio="9:16", duration="10s")


@pytest.mark.asyncio
async def test_request_video_params_keep_all_but_the_length(
    fake_gen, fake_eval, videos_per_image
):
    videos_per_image(3)
    fake_gen.video(CLIP)
    params = VideoParams(aspect_ratio="16:9", resolution="1080p", deadline_s=60)
    await collect(request(video=params))
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert len(calls) == 3
    assert all(
        c["params"] == dataclasses.replace(params, duration="8s") for c in calls
    )


@pytest.mark.asyncio
async def test_product_photos_go_as_named_closeups(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    products = [product_png(1)]
    req = one(
        product_images=products,
        classification=classification("full_body", "bag"),
    )
    await collect(req)
    assert fake_gen.calls[0]["raw_parts"] == [TRYON, FACE, *products]
    (closeup,) = planner.calls[0][0].closeups
    assert (closeup.index, closeup.name, closeup.worn) == (
        2,
        "bag",
        "in the person's hand",
    )


def video_call(fake_gen):
    (call,) = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    return call


@pytest.mark.asyncio
async def test_photos_the_classifier_fails_on_all_go(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    fake_gen.json(GenerationError("classifier down"))
    photos = [product_png(1), product_png(2)]  # categories unknown
    events = await collect(one(product_images=photos))
    assert video_call(fake_gen)["raw_parts"] == [TRYON, FACE, *photos]
    assert events[0].type == "progress"  # no classified event


@pytest.mark.asyncio
async def test_a_classified_garment_sends_no_photo_but_is_judged(
    fake_gen, fake_eval
):
    """An API classification (no uploads): only the bag's photo goes; the
    coat is judged against its own photo."""
    fake_gen.video(CLIP)
    photos = [product_png(1), product_png(2)]
    meta = Classification(
        "outfit",
        [
            Product("A", "full_body_outer", "coat", []),
            Product("B", "bag", "", []),
        ],
        "full_body",
    )
    await collect(one(product_images=photos, classification=meta))
    assert fake_gen.calls[0]["raw_parts"] == [TRYON, FACE, photos[1]]
    judged = {
        (c["product_filter"], *c["references"]) for c in fake_eval.judge_calls
    }
    assert judged == {("full_body_outer", photos[0]), ("bag", photos[1])}


@pytest.mark.asyncio
async def test_a_close_up_product_also_sends_its_zoomed_detail(
    fake_gen, fake_eval, planner, zoom
):
    fake_gen.video(CLIP)
    zoom.found = [Detail("logo", "", "front", "logo on the flap", b"zoom")]
    planner.plan = Plan([Shot("walk", 4.0), Shot("detail", 4.0, "bag")])
    req = one(
        product_images=[product_png(1)],
        classification=classification("full_body", "bag"),
    )
    events = await collect(req)
    call = fake_gen.calls[0]
    assert call["raw_parts"] == [TRYON, FACE, product_png(1), b"zoom 2K"]
    assert (
        "<IMAGE_3> is a close-up of the bag (logo on the flap)"
        in (call["prompt"])
    )
    assert events[0].data["references"] == 4


@pytest.mark.asyncio
async def test_the_back_view_is_judged_when_the_plan_turns(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    planner.plan = TURN
    fake_eval.views = ["front", "front", "back"]
    await collect(one(back_image=BACK))
    judged = {
        FRAMES.index(c["target"]): c["references"]
        for c in fake_eval.judge_calls
    }
    assert judged == {0: [TRYON], 1: [TRYON], 2: [BACK]}
    assert (
        "until the back faces the camera, matching <IMAGE_1>"
        in (fake_gen.calls[0]["prompt"])
    )


@pytest.mark.asyncio
async def test_the_back_view_is_sent_and_seen_by_the_blind_spot_check(
    fake_gen, fake_eval
):
    fake_gen.video(CLIP)
    await collect(one(back_image=BACK))
    assert fake_gen.calls[0]["raw_parts"] == [TRYON, BACK, FACE]
    # No frame faces away: every frame is judged against the front.
    judged = [c["references"] for c in fake_eval.judge_calls]
    assert judged == [[TRYON]] * 3
    assert fake_eval.unseen_calls == [[TRYON, BACK]]  # the back is seen


@pytest.mark.asyncio
async def test_a_blind_spot_rejects_before_the_judges(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    fake_eval.unseen = [3, 4]
    (v,) = variations(await collect(one()))
    assert v.status == "rejected"
    assert v.verdict.description == "shows what no reference shows: the back"
    assert v.verdict.details["unseen_frames"] == [3, 4]
    assert "checks" not in v.verdict.details


@pytest.mark.asyncio
async def test_a_failed_blind_spot_check_does_not_reject(
    fake_gen, fake_eval, monkeypatch
):
    def down(clip, references):
        raise GenerationError("judge down")

    monkeypatch.setattr(unseen, "unseen_frames", down)
    fake_gen.video(CLIP)
    (v,) = variations(await collect(one()))
    assert v.status == "ready"
    assert len(fake_eval.judge_calls) == 3


@pytest.mark.asyncio
async def test_same_person_then_each_product_is_judged(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    (v,) = variations(await collect(one(product_images=[product_png(1)])))
    (label,) = fake_eval.label_calls
    assert label["frames"] == FRAMES and label["strategy"] == "gemini"
    assert label["references"] == {"front": TRYON}
    judged = sorted(FRAMES.index(c["target"]) for c in fake_eval.judge_calls)
    assert judged == [0, 1, 2]
    assert all(
        c["references"] == [product_png(1)] and c["product_filter"] == "product"
        for c in fake_eval.judge_calls
    )
    assert fake_eval.identity_calls == [(FRAMES, FACE)]
    assert v.status == "ready"
    assert v.verdict.details["identity"]["same_person"] is True
    assert sorted(v.verdict.details) == [
        "checks",
        "frames",
        "identity",
        "score",
        "unseen_frames",
    ]
    assert v.score == quality_score(True, 0) == 100.0


@pytest.mark.asyncio
async def test_a_rejected_check_rejects(fake_gen, fake_eval):
    fake_gen.video(CLIP)
    fake_eval.verdicts[(None, 0)] = Verdict(False, 1, "logo missing")
    (v,) = variations(await collect(one()))
    assert v.status == "rejected"
    assert v.verdict.description == "rejected: outfit at 0s: logo missing"
    # one defect listed for the product: one imperfection
    assert v.score == quality_score(False, 1)


@pytest.mark.asyncio
async def test_no_face_in_tryon_image_skips_the_identity_check(
    fake_gen, fake_eval
):
    fake_gen.video(CLIP)
    fake_eval.face_crop = None
    events = await collect(one())
    (v,) = variations(events)
    assert events[0].data["references"] == 1
    assert fake_eval.identity_calls == []
    assert v.status == "ready"
    assert v.verdict.details["identity"] is None
    assert "face check" not in v.verdict.description
    assert v.score == quality_score(True, 0) == 100.0


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 4])
async def test_videos_per_image_whatever_the_number_of_variations(
    fake_gen, fake_eval, videos_per_image, count
):
    """Animate-only makes ``videos_per_image`` videos, like each image of an
    end-to-end run; ``num_variations`` is the number of try-on images."""
    videos_per_image(count)
    fake_gen.video(CLIP)
    events = await collect(request(policy=RunPolicy(num_variations=3)))
    found = variations(events)
    assert sorted(v.index for v in found) == list(range(count))
    assert all(v.extra == {"stage": "video"} for v in found)
    assert sorted(events[-1].ranked_indices) == list(range(count))


@pytest.mark.asyncio
async def test_no_video_made_is_an_error_not_complete(fake_gen, fake_eval):
    fake_gen.video(GenerationError("quota"))
    events = await collect(request())
    assert [v.status for v in variations(events)] == ["error"]
    last = events[-1]
    assert isinstance(last, ErrorEvent)
    assert last.message == "No video was produced: quota"
    assert last.retryable is True
    assert not any(isinstance(e, CompleteEvent) for e in events)


@pytest.mark.asyncio
async def test_generation_error_in_one_variation(
    fake_gen, fake_eval, videos_per_image
):
    videos_per_image(3)
    fake_gen.video([GenerationError("boom"), CLIP])
    events = await collect(request())
    found = variations(events)
    assert sorted(v.status for v in found) == ["error", "ready", "ready"]
    assert events[-1].best_index is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["face", "planner"])
@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (GenerationError("vision down"), "vision down"),  # for the user
        (RuntimeError("key=secret at /srv/x.py"), SERVER_FAILED),
    ],
)
async def test_preparation_failure_yields_one_error_event(
    fake_gen, fake_eval, planner, where, error, shown
):
    if where == "face":
        fake_eval.face_crop = error
    else:
        planner.plan = error
    events = await collect(request())
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].message == shown
    assert fake_gen.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw",
    [
        {"tryon_image": b""},
        {"motion": "custom"},
        {"motion": "custom", "prompt": "  "},
        {"motion": "catwalk"},
        {"motion": "turnaround"},
        {"video": VideoParams(aspect_ratio="1:1")},
    ],
)
async def test_input_errors(fake_gen, fake_eval, kw):
    kw.setdefault("tryon_image", TRYON)
    with pytest.raises(InputError):
        await collect(VideoTryOnRequest(**kw))
    assert fake_gen.calls == []
    assert fake_eval.identity_calls == []
