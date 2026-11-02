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

"""End-to-end Video Try-On: the image try-on stage, then each passing image
animated at once (its own back view first when there can be one)."""

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Literal

import pytest

from genmedia4commerce.core.errors import (
    SERVER_FAILED,
    GenerationError,
    InputError,
)
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams, VideoParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
    Verdict,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import pipeline as tryon_pipeline
from genmedia4commerce.workflows.video_tryon import branch
from genmedia4commerce.workflows.video_tryon.pipeline import run_video_tryon
from genmedia4commerce.workflows.video_tryon.types import (
    Plan,
    Shot,
    VideoTryOnRequest,
)
from tests.conftest import trace_file, trace_lines, tracing
from tests.workflows.video_tryon.conftest import (
    CLIP,
    FACE,
    TRYON,
    classification,
    product_png,
)

pytestmark = pytest.mark.asyncio

PERSON = png(40, 80, (5, 5, 5))
FRONTS = [png(30, 60, (200, 10 * i, 0)) for i in range(3)]
# The back views drawn from front k: BACKS[k][j].
BACKS = [
    [png(30, 60, (0, 10 * k, 200 - j)) for j in range(2)] for k in range(3)
]
PRODUCTS = [product_png(1), product_png(2)]
ONE = RunPolicy(num_variations=1)
WORN = png(30, 30, (1, 200, 1))  # product A worn by someone
CROP = png(30, 30, (2, 200, 2))  # product B cut from the reference model
SHOE_FRONT, SHOE_BACK = product_png(3), product_png(4)  # product C, flat
UPLOADS = [PRODUCTS[0], WORN, PRODUCTS[1], SHOE_BACK, SHOE_FRONT]
MODEL = png(20, 29, (0, 109, 0))  # a reference model photo
BACK_PHOTO = product_png(5)  # a back photo of product A
COAT, TROUSERS, BAG, SHOES = (product_png(i) for i in range(10, 14))
LOOK = [COAT, TROUSERS, BAG, SHOES]


def front(
    i: int, status: Literal["ready", "rejected", "error"] = "ready"
) -> VariationEvent:
    verdict = Verdict(publish=status == "ready")
    return VariationEvent(
        i, status, FRONTS[i], verdict, 50.0, extra={"view": "front"}
    )


def back(k: int, j: int, **extra) -> VariationEvent:
    """Back variation ``j`` drawn from front ``k``, as ``back_view`` sends it."""
    return VariationEvent(
        j,
        "ready",
        BACKS[k][j],
        Verdict(True),
        50.0,
        extra={"view": "back", **extra},
    )


def ref(image, view="front", rank=1, presentation="flat", source="upload"):
    return ProductRef(image, view, rank, presentation, source)


def outfit() -> Classification:
    """Products whose hero (the try-on puts it first) is worn (A), flat
    with a crop (B), flat (C)."""
    a = [ref(WORN, presentation="worn"), ref(PRODUCTS[0])]
    b = [ref(PRODUCTS[1]), ref(CROP, source="model_crop")]
    c = [ref(SHOE_FRONT), ref(SHOE_BACK, view="back")]
    products = [
        Product("A", "top", "shirt", a),
        Product("B", "bottom", "jeans", b),
        Product("C", "footwear", "shoes", c),
    ]
    return Classification("outfit", products, "full_body")


def look() -> Classification:
    """A coat, trousers, a bag and shoes (``LOOK``), one flat upload each."""
    products = [
        Product("A", "full_body_outer", "sleeveless coat", [ref(COAT)]),
        Product("B", "bottom", "trousers", [ref(TROUSERS)]),
        Product("C", "bag", "tote", [ref(BAG)]),
        Product("D", "footwear", "loafers", [ref(SHOES)]),
    ]
    return Classification("outfit", products, "full_body")


def image_stage(
    *variations,
    framing="full_body",
    best=1,
    ranked=(1, 0, 2),
    classified=None,
):
    """Events of a fake ``run_tryon`` run (fresh objects each call); by
    default only front 1 passes."""
    payload = {"mode": "single", "framing": framing, "products": []}
    fronts = variations or (
        front(0, "rejected"),
        front(1),
        front(2, "rejected"),
    )
    return [
        ClassifiedEvent(payload, classification=classified),
        ProgressEvent("preprocessed", "Generating the front view"),
        *fronts,
        CompleteEvent(list(ranked), best),
    ]


# A single product with a back upload: each animated front gets a back view.
WITH_BACK = Classification(
    "single",
    [Product("A", "top", "shirt", [ref(PRODUCTS[0]), ref(BACK_PHOTO, "back")])],
    "upper_body",
)


@pytest.fixture
def stage_one(monkeypatch):
    """Install a fake ``run_tryon`` yielding the given events; ``back``:
    it prepares a single product with a back upload."""

    def install(events, *, back=False):
        requests = []

        async def run(req, prepared=None):
            requests.append(req)
            if back:
                prepared.ctx = SimpleNamespace(classification=WITH_BACK)
                prepared.policy = req.policy  # as the real run sets it
            for event in events:
                yield event

        run.requests = requests
        monkeypatch.setattr(tryon_pipeline, "run_tryon", run)
        return run

    return install


@pytest.fixture
def backs(monkeypatch):
    """Fake ``tryon_pipeline.back_view``: two back variations of the front,
    ranked on the last one (``best``: the best one, None: none passed);
    records the fronts drawn from."""
    drawn = SimpleNamespace(fronts=[], best=1, counts=[])

    async def back_view(prepared, image):
        drawn.fronts.append(image)
        drawn.counts.append(prepared.policy.num_variations)
        k = FRONTS.index(image)
        yield back(k, 0)
        yield back(k, 1, ranked=[1, 0], best_index=drawn.best)

    monkeypatch.setattr(tryon_pipeline, "back_view", back_view)
    return drawn


def request(**kw) -> VideoTryOnRequest:
    kw.setdefault("policy", RunPolicy(num_variations=2))
    kw.setdefault("product_images", PRODUCTS)
    return VideoTryOnRequest(person_image=PERSON, **kw)


async def collect(req):
    return [event async for event in run_video_tryon(req)]


def selections(events):
    return [
        e
        for e in events
        if isinstance(e, ProgressEvent) and e.stage == "image_selected"
    ]


def video_calls(fake_gen):
    return [c for c in fake_gen.calls if c["fn"] == "generate_video"]


def variations(events, **extra):
    """The variation events whose ``extra`` has these items."""
    return [
        e
        for e in events
        if isinstance(e, VariationEvent)
        and all(e.extra.get(k) == v for k, v in extra.items())
    ]


async def test_stream_order_and_stage_tags(fake_gen, fake_eval, stage_one):
    fake_gen.video(CLIP)
    stage_one(image_stage())
    events = await collect(request())
    kinds = [
        e.stage if isinstance(e, ProgressEvent) else e.type for e in events
    ]
    assert kinds == [
        "classified",
        "preprocessed",
        "variation",
        "variation",
        "variation",
        "image_selected",
        "plan",
        "variation",
        "variation",
        "complete",
    ]
    assert [e.message for e in events if isinstance(e, ProgressEvent)] == [
        "Generating the front view",
        "Animating try-on variation 2",
        "Animation of try-on variation 2: pose, 8 s",
    ]
    image, video = events[2:5], events[7:9]
    assert [v.extra for v in image] == [{"view": "front", "stage": "image"}] * 3
    assert [v.media for v in image] == FRONTS
    assert [v.extra for v in video] == [
        {"stage": "video", "source_index": 1}
    ] * 2
    assert sorted(v.index for v in video) == [0, 1]
    assert all(v.media == CLIP for v in video)
    assert events[6].data["source_index"] == 1  # the plan
    assert events[6].data["videos"] == 2
    assert sum(isinstance(e, CompleteEvent) for e in events) == 1
    assert sorted(events[-1].ranked_indices) == [0, 1]


async def test_image_selected_and_main_reference(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    stage_one(image_stage())
    events = await collect(request(policy=ONE))
    (selected,) = selections(events)
    assert selected.data == {"front_index": 1, "back_index": None}
    assert selected.message == "Animating try-on variation 2"
    calls = video_calls(fake_gen)
    assert [c["raw_parts"] for c in calls] == [[FRONTS[1], FACE, *PRODUCTS]] * 2
    assert fake_eval.unseen_calls == [[FRONTS[1]]] * 2


async def test_stage_one_request(fake_gen, fake_eval, stage_one):
    fake_gen.video(CLIP)
    run = stage_one(image_stage())
    image = ImageParams(image_size="4K")
    req = request(
        reference_model_image=MODEL,
        style_guidelines="jackets closed",
        scenario="a beach",
        complete_look=False,
        upscale_crops=True,
        image=image,
    )
    await collect(req)
    (sent,) = run.requests
    assert sent.product_images == PRODUCTS
    assert sent.person_image == PERSON
    assert sent.reference_model_image == MODEL
    assert (sent.style_guidelines, sent.scenario) == (
        "jackets closed",
        "a beach",
    )
    assert sent.generate_back is False  # each animated front draws its own
    assert sent.complete_look is False
    assert sent.upscale_crops is True
    assert sent.policy is req.policy
    assert sent.image is image


@pytest.mark.parametrize(
    ("video", "image"),
    [
        (None, ImageParams(aspect_ratio="9:16")),
        (VideoParams(aspect_ratio="9:16"), ImageParams(aspect_ratio="9:16")),
        (VideoParams(aspect_ratio="16:9"), ImageParams()),
    ],
)
async def test_stage_one_image_follows_a_portrait_video(
    fake_gen, fake_eval, stage_one, video, image
):
    """9:16 video: a 9:16 try-on image; 16:9 keeps the 3:4 portrait."""
    fake_gen.video(CLIP)
    run = stage_one(image_stage())
    await collect(request(video=video))
    (sent,) = run.requests
    assert sent.image == image


async def test_a_passing_front_gets_its_own_back_view_then_its_videos(
    fake_gen, fake_eval, stage_one, backs, planner
):
    planner.plan = Plan([Shot("pose", 4.0), Shot("turn_back", 5.0)])
    fake_gen.video(CLIP)
    stage_one(image_stage(), back=True)
    events = await collect(request(policy=ONE))
    assert backs.fronts == [FRONTS[1]]  # drawn from the passing front only
    assert backs.counts == [1]  # one back image per animated image
    kinds = [
        e.stage if isinstance(e, ProgressEvent) else e.type for e in events
    ]
    assert kinds[5:] == [
        "back",
        "variation",
        "variation",
        "image_selected",
        "plan",
        "variation",
        "variation",
        "complete",
    ]
    drawn = [e for e in events if e.type == "variation" and e.media in BACKS[1]]
    assert [e.extra["stage"] for e in drawn] == ["image"] * 2
    assert [e.extra["source_index"] for e in drawn] == [1, 1]
    assert drawn[-1].extra["ranked"] == [1, 0]
    assert drawn[-1].extra["best_index"] == 1
    assert events[5].message == "Generating the back view of try-on variation 2"
    (selected,) = selections(events)
    assert selected.data == {"front_index": 1, "back_index": 1}
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts == [FRONTS[1], BACKS[1][1], FACE, *PRODUCTS]
    assert fake_eval.unseen_calls == [[FRONTS[1], BACKS[1][1]]] * 2


async def test_no_ready_back_animates_the_front_alone(
    fake_gen, fake_eval, stage_one, backs
):
    backs.best = None
    fake_gen.video(CLIP)
    stage_one(image_stage(), back=True)
    events = await collect(request(policy=ONE))
    (selected,) = selections(events)
    assert selected.data == {"front_index": 1, "back_index": None}
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts == [FRONTS[1], FACE, *PRODUCTS]


async def test_back_views_are_numbered_across_the_images(
    fake_gen, fake_eval, stage_one, backs
):
    fake_gen.video(CLIP)
    stage_one(image_stage(front(0), front(1), front(2, "rejected")), back=True)
    events = await collect(request())
    assert sorted(backs.fronts) == sorted(FRONTS[:2])
    drawn = variations(events, view="back")
    assert sorted(e.index for e in drawn) == [0, 1, 2, 3]
    chosen = {
        e.data["front_index"]: e.data["back_index"] for e in selections(events)
    }
    for k, index in chosen.items():
        (best,) = [e for e in drawn if e.index == index]
        assert best.media == BACKS[k][1]
        assert best.extra["source_index"] == k
    for call in video_calls(fake_gen):
        k = FRONTS.index(call["raw_parts"][0])
        assert call["raw_parts"][1] == BACKS[k][1]


async def test_an_outfit_draws_no_back_view(
    fake_gen, fake_eval, stage_one, backs
):
    fake_gen.video(CLIP)
    stage_one(image_stage(classified=outfit()))
    events = await collect(request(product_images=UPLOADS, policy=ONE))
    assert backs.fronts == []
    assert not any(
        isinstance(e, ProgressEvent) and e.stage == "back" for e in events
    )
    assert isinstance(events[-1], CompleteEvent)


async def test_framing_comes_from_the_classified_payload(
    fake_gen, fake_eval, stage_one, planner
):
    fake_gen.video(CLIP)
    stage_one(image_stage(framing="footwear"))
    await collect(request(classification=classification("head"), policy=ONE))
    assert planner.calls[0][0].framing == "footwear"


async def test_product_photos_are_the_ones_the_tryon_used(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    stage_one(image_stage(classified=outfit()))
    await collect(request(product_images=UPLOADS, policy=ONE))
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts == [FRONTS[1], FACE, SHOE_FRONT]  # the top and jeans: none


async def test_garments_send_no_photo_but_every_product_is_checked(
    fake_gen, fake_eval, stage_one, planner, zoom
):
    """Garments come from the try-on image: their photos made the video
    model redraw them (a sleeveless coat got sleeves)."""
    zoom.found = [Detail("logo", "", "front", "logo on the flap", b"zoom")]
    planner.plan = Plan([Shot("walk", 4.0), Shot("detail", 4.0, "bag")])
    fake_gen.video(CLIP)
    stage_one(image_stage(classified=look()))
    await collect(request(product_images=LOOK, policy=ONE))
    closeups = planner.calls[0][0].closeups  # the plan's close-up menu
    assert [(c.index, c.name) for c in closeups] == [(2, "bag"), (3, "shoes")]
    # each photo searched ahead, told its product's name
    assert sorted((i, name) for i, _, name in zoom.calls) == sorted(
        [([BAG], "tote"), ([SHOES], "loafers")]
    )
    assert [[d.where for d in found] for found in zoom.upscaled] == [
        ["logo on the flap"]  # the bag's, the close-up product
    ]
    call = video_calls(fake_gen)[0]
    assert call["raw_parts"] == [FRONTS[1], FACE, BAG, SHOES, b"zoom 2K"]
    assert "coat" not in call["prompt"] and "bottoms" not in call["prompt"]
    judged = {
        (c["product_filter"], *c["references"]) for c in fake_eval.judge_calls
    }
    assert judged == {
        ("full_body_outer", COAT),
        ("bottom", TROUSERS),
        ("bag", BAG),
        ("footwear", SHOES),
    }


async def test_the_tryon_photos_are_named_after_their_products(
    fake_gen, fake_eval, stage_one, planner
):
    fake_gen.video(CLIP)
    stage_one(image_stage(classified=look()))
    await collect(request(product_images=LOOK, policy=ONE))
    closeups = planner.calls[0][0].closeups
    assert [(c.index, c.name, c.worn) for c in closeups] == [
        (2, "bag", "in the person's hand"),
        (3, "shoes", "on the person's feet"),
    ]


async def test_details_are_searched_during_the_image_stage(
    fake_gen, fake_eval, stage_one, planner, zoom, monkeypatch
):
    import threading

    from genmedia4commerce.workflows import details

    searched = threading.Event()
    upscale_flags = []

    def find(images, *, limit=2, upscale=True, product=""):
        upscale_flags.append(upscale)
        searched.set()
        return zoom(images, limit=limit, product=product)

    def plan(refs, request=None):  # the plan waits for the search to start
        assert searched.wait(timeout=5), "the search waited for the plan"
        return planner(refs, request)

    monkeypatch.setattr(details, "find", find)
    monkeypatch.setattr(branch, "plan_animation", plan)
    fake_gen.video(CLIP)
    stage_one(image_stage(classified=look()))
    await collect(request(product_images=LOOK, policy=ONE))
    # every accessory and shoe photo the try-on used, not the garments'
    assert sorted(images for images, *_ in zoom.calls) == sorted(
        [[BAG], [SHOES]]
    )
    assert upscale_flags == [False] * 2  # no image-model call during it


async def test_single_product_sends_its_best_photo(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    refs = [ref(WORN, presentation="worn"), ref(PRODUCTS[0])]  # hero first
    single = Classification(
        "single", [Product("A", "bag", "tote", refs)], "full_body"
    )
    stage_one(image_stage(classified=single))
    req = request(product_images=[PRODUCTS[0], WORN], policy=ONE)
    await collect(req)
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts[:2] == [FRONTS[1], FACE]
    assert parts[-1] == WORN and PRODUCTS[0] not in parts


async def test_a_product_drawn_from_a_look_crop_sends_its_upload(
    fake_gen, fake_eval, stage_one
):
    """The try-on's hero of the bag was its look-photo crop: the video
    gets its sharp upload, never the crop."""
    fake_gen.video(CLIP)
    refs = [ref(CROP, presentation="worn", source="model_crop")]
    refs.append(ref(PRODUCTS[0]))
    single = Classification(
        "single", [Product("A", "bag", "tote", refs)], "full_body"
    )
    stage_one(image_stage(classified=single))
    await collect(request(product_images=[PRODUCTS[0]], policy=ONE))
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts[-1] == PRODUCTS[0] and CROP not in parts


async def test_no_classification_keeps_the_request_photos(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    stage_one(image_stage())
    await collect(request(policy=ONE))
    parts = video_calls(fake_gen)[0]["raw_parts"]
    assert parts == [FRONTS[1], FACE, *PRODUCTS]


async def test_with_no_published_front_the_best_scoring_one_is_animated(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    low = VariationEvent(0, "rejected", FRONTS[0], Verdict(False), 40.0)
    high = VariationEvent(1, "rejected", FRONTS[1], Verdict(False), 70.0)
    for v in (low, high):
        v.extra = {"view": "front"}
    stage_one(image_stage(low, high, best=None, ranked=(1, 0)))
    events = await collect(request(policy=ONE))
    (selected,) = selections(events)
    assert selected.data["front_index"] == 1
    assert selected.message == (
        "Animating try-on variation 2 (none passed every check)"
    )
    assert [c["raw_parts"][0] for c in video_calls(fake_gen)] == [FRONTS[1]] * 2
    videos = variations(events, stage="video")
    assert [v.extra["source_index"] for v in videos] == [1, 1]
    assert events.index(selected) > events.index(high)  # after the images
    assert isinstance(events[-1], CompleteEvent)


async def test_no_image_at_all_is_an_error(fake_gen, fake_eval, stage_one):
    failed = VariationEvent(0, "error", None, None, 0.0, error="quota")
    failed.extra = {"view": "front"}
    stage_one(image_stage(failed, best=None, ranked=(0,)))
    events = await collect(request())
    last = events[-1]
    assert isinstance(last, ErrorEvent)
    assert last.message == "Image try-on produced no image"
    assert last.retryable is True
    assert video_calls(fake_gen) == []


async def test_stage_one_error_is_forwarded_and_stops(
    fake_gen, fake_eval, stage_one
):
    payload = {"mode": "single", "framing": "head", "products": []}
    stage_one([ClassifiedEvent(payload), ErrorEvent("quota", retryable=True)])
    events = await collect(request())
    assert [e.type for e in events] == ["classified", "error"]
    assert (events[-1].message, events[-1].retryable) == ("quota", True)
    assert video_calls(fake_gen) == []


async def test_shortcut_skips_the_image_stage(fake_gen, fake_eval, stage_one):
    fake_gen.video(CLIP)
    run = stage_one([])
    req = VideoTryOnRequest(
        tryon_image=TRYON,
        person_image=PERSON,
        product_images=[product_png(1)],
        policy=RunPolicy(num_variations=2),
    )
    events = await collect(req)
    assert run.requests == []
    assert [e.type for e in events] == [
        "progress",
        "variation",
        "variation",
        "complete",
    ]
    assert events[0].stage == "plan"
    assert all(e.extra == {"stage": "video"} for e in events[1:3])
    assert video_calls(fake_gen)[0]["raw_parts"][0] == TRYON


async def test_shortcut_framing_from_request_classification(
    fake_gen, fake_eval, planner
):
    fake_gen.video(CLIP)
    req = VideoTryOnRequest(
        tryon_image=TRYON,
        classification=classification("footwear"),
        policy=ONE,
    )
    await collect(req)
    assert planner.calls[0][0].framing == "footwear"


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"person_image": PERSON},
        {"product_images": [product_png(1)]},
        {"person_image": b"", "product_images": [product_png(1)]},
        {"person_image": PERSON, "product_images": [b""]},
        {
            "person_image": PERSON,
            "product_images": [product_png(i) for i in range(21)],
        },
        {"tryon_image": b""},
        {"person_image": PERSON, "product_images": [product_png(1)]}
        | {"motion": "custom"},
        {"person_image": PERSON, "product_images": [product_png(1)]}
        | {"video": VideoParams(aspect_ratio="1:1")},
    ],
)
async def test_input_errors_before_any_event(
    fake_gen, fake_eval, stage_one, kw
):
    run = stage_one(image_stage())
    with pytest.raises(InputError):
        await collect(VideoTryOnRequest(**kw))
    assert run.requests == []
    assert fake_gen.calls == []


async def test_debug_trace_saves_the_selection_and_labels_the_video_stage(
    fake_gen, fake_eval, stage_one, debug_dir
):
    fake_gen.video(CLIP)
    stage_one(image_stage())
    with tracing() as root:
        await collect(request())
    assert trace_file(root, "_selected-front.png").read_bytes() == FRONTS[1]
    (back,) = [x for x in trace_lines(root) if x["label"] == "selected-back"]
    assert back["file"] is None
    assert back["labels"]["source"] == 1  # the branch of try-on image 1
    for i in range(2):
        verdict = trace_file(root, f"_v{i}a1_video_verdict.json")
        assert json.loads(verdict.read_text())["verdict"]["publish"] is True


# --- each passing image is animated at once ---------------------------------


async def test_a_passing_image_starts_its_videos_before_a_slower_one_finishes(
    fake_gen, fake_eval, monkeypatch
):
    started = threading.Event()  # a video call was made

    def make_video(*_args, **_kwargs):
        started.set()
        return CLIP

    async def run(req, prepared=None):
        payload = {"mode": "single", "framing": "full_body", "products": []}
        yield ClassifiedEvent(payload)
        yield front(0)
        for _ in range(500):  # front 1 is slow: done once a video started
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        run.started_first = started.is_set()
        yield front(1, "rejected")
        yield CompleteEvent([0, 1], 0)

    fake_gen.video(make_video)
    monkeypatch.setattr(tryon_pipeline, "run_tryon", run)
    events = await collect(request())
    assert run.started_first, "the videos waited for every image"
    slow = variations(events, view="front")[1]
    plan = next(e for e in events if getattr(e, "stage", "") == "plan")
    assert events.index(plan) < events.index(slow)
    assert isinstance(events[-1], CompleteEvent)


async def test_each_passing_image_gets_two_videos_numbered_across_the_run(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(CLIP)
    stage_one(image_stage(front(0), front(1, "rejected"), front(2)))
    events = await collect(request())
    assert [e.data for e in selections(events)] == [
        {"front_index": 0, "back_index": None},
        {"front_index": 2, "back_index": None},
    ]
    plans = [e for e in events if getattr(e, "stage", "") == "plan"]
    assert sorted(p.data["source_index"] for p in plans) == [0, 2]
    videos = variations(events, stage="video")
    assert sorted(v.index for v in videos) == [0, 1, 2, 3]
    by_source = sorted(v.extra["source_index"] for v in videos)
    assert by_source == [0, 0, 2, 2]
    sent = sorted(
        FRONTS.index(c["raw_parts"][0]) for c in video_calls(fake_gen)
    )
    assert sent == [0, 0, 2, 2]
    last = events[-1]
    assert isinstance(last, CompleteEvent)
    assert sorted(last.ranked_indices) == [0, 1, 2, 3]
    assert last.best_index in last.ranked_indices


async def test_the_detail_search_is_shared_by_every_image(
    fake_gen, fake_eval, stage_one, zoom
):
    fake_gen.video(CLIP)
    stage_one(image_stage(front(0), front(1), classified=look()))
    await collect(request(product_images=LOOK))
    assert len(zoom.calls) == 2  # once per bag and shoes photo, not per image


async def test_no_video_made_is_an_error_not_complete(
    fake_gen, fake_eval, stage_one
):
    fake_gen.video(GenerationError("quota"))
    stage_one(image_stage(front(0), front(1)))
    events = await collect(request())
    videos = variations(events, stage="video")
    assert [v.status for v in videos] == ["error"] * 4
    last = events[-1]
    assert isinstance(last, ErrorEvent)
    assert last.message == "No video was produced: quota"
    assert last.retryable is True
    assert not any(isinstance(e, CompleteEvent) for e in events)


async def test_an_image_that_cannot_be_animated_reports_its_videos_as_errors(
    fake_gen, fake_eval, stage_one, planner, monkeypatch
):
    def plan(refs, request=None):
        if refs.images[0] == FRONTS[0]:
            raise RuntimeError("planner down")
        return planner(refs, request)

    monkeypatch.setattr(branch, "plan_animation", plan)
    fake_gen.video(CLIP)
    stage_one(image_stage(front(0), front(1)))
    events = await collect(request())
    failed = variations(events, stage="video", source_index=0)
    assert [v.status for v in failed] == ["error", "error"]
    # The generic message, not the exception's text.
    assert all(v.error.endswith(SERVER_FAILED) for v in failed)
    made = variations(events, stage="video", source_index=1)
    assert [v.status for v in made] == ["ready", "ready"]
    videos = failed + made
    assert sorted(v.index for v in videos) == [0, 1, 2, 3]
    last = events[-1]
    assert isinstance(last, CompleteEvent)
    assert last.best_index in {v.index for v in made}


async def test_closing_the_stream_stops_every_branch(
    fake_gen, fake_eval, monkeypatch
):
    release = threading.Event()
    closed = []

    def make_video(*_args, **_kwargs):
        release.wait(5)
        return CLIP

    async def run(req, prepared=None):
        try:
            yield ClassifiedEvent({"framing": "full_body"})
            yield front(0)
            await asyncio.Event().wait()  # the other images never finish
        finally:
            closed.append(True)

    fake_gen.video(make_video)
    monkeypatch.setattr(tryon_pipeline, "run_tryon", run)
    stream = run_video_tryon(request())
    try:
        async for event in stream:
            if getattr(event, "stage", "") == "plan":
                break  # its videos are being made
        await stream.aclose()
        for _ in range(3):
            await asyncio.sleep(0)
        others = asyncio.all_tasks() - {asyncio.current_task()}
        assert others == set()
        assert closed == [True]  # the image stage was closed too
    finally:
        release.set()
