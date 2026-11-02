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
import io
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import media
from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import SERVER_FAILED, GenerationError
from genmedia4commerce.core.gen import VideoParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import mp4_stub, png
from genmedia4commerce.core.types import StoredVideo, Verdict, quality_score
from genmedia4commerce.eval import motion, video_fidelity
from genmedia4commerce.workflows.spin import pipeline as spin_pipeline
from genmedia4commerce.workflows.spin import (
    product_check,
    shoe_check,
    shoe_refs,
)
from genmedia4commerce.workflows.spin.pipeline import run_spin
from genmedia4commerce.workflows.spin.prompt import shoe_prompt
from genmedia4commerce.workflows.spin.types import SpinRequest
from tests.conftest import trace_file, tracing

pytestmark = pytest.mark.asyncio

CLIP = mp4_stub(frames=12)
SPIN = core_video.to_webp(CLIP)  # every spin is an animated WebP
REVERSED = core_video.encode_frames([png(8, 8)] * 2, fps=24)  # re-encoded
HALF = b"first-half-turn"  # never decoded: its checks are faked
TURN_FRAMES = [(0, b"frame-0"), (12, b"frame-12")]
TURN_LABELS = ["right", "front_right"]
# Shoe references: a wide side view, a tall top view, a square back view and
# a portrait three-quarter view (the right side view sets an auto ratio).
SIDE, TOP, BACK, PAIR = (
    png(300, 120),
    png(100, 300),
    png(200, 200),
    png(96, 134),
)


async def _collect(req: SpinRequest) -> list:
    return [event async for event in run_spin(req)]


def _types(events) -> list[str]:
    return [e.type for e in events]


def _classified(fake_gen, product_type, views):
    fake_gen.json(
        {
            "product_type": product_type,
            "description": "a black smartphone",
            "classifications": [
                {"index": i, "view": view, "rank": rank}
                for i, (view, rank) in enumerate(views)
            ],
        }
    )


@pytest.fixture
def fidelity(monkeypatch):
    """Record evaluate_video_fidelity calls; publish by default."""
    calls: list[dict] = []
    result = {"verdict": Verdict(True, description="faithful")}

    def fake(video, references, **kw):
        calls.append({"video": video, "references": dict(references), **kw})
        return Verdict(**vars(result["verdict"]) | {"details": {}})

    monkeypatch.setattr(video_fidelity, "evaluate_video_fidelity", fake)
    return SimpleNamespace(calls=calls, result=result)


def _staged(monkeypatch, fake_gen) -> dict:
    """Fake the two half turns and the rotation/glitch checks.

    The first half turn (a stored "half") passes its rotation check unless a
    test sets ``half_rotation`` (a queue); ``rotation`` / ``glitch`` apply to
    the whole spin, its continuation. The half never gets a glitch check.
    """
    state = {
        "rotation": "clockwise",
        "glitch": Verdict(True),
        "glitches": [],
        "half_rotation": [],
        "halves": 0,
        "labels": None,  # the frame labels (None: all unclear)
    }

    def video(images, prompt, *, params=None, store=False, extends=None):
        if store:
            state["halves"] += 1
            return StoredVideo(HALF, f"turn-1.{state['halves']}", None)
        return StoredVideo(CLIP, "turn-2", extends)

    fake_gen.video(video)

    def rotation(video):
        if video == HALF:
            queue = state["half_rotation"]
            return queue.pop(0) if queue else "clockwise"
        return state["rotation"]

    monkeypatch.setattr(motion, "check_rotation", rotation)

    def glitches(video):
        assert video != HALF, "the first half gets no glitch check"
        state["glitches"].append(video)
        return state["glitch"]

    monkeypatch.setattr(motion, "detect_glitches", glitches)

    def labels(frames, refs):
        return state["labels"] or ["unclear"] * len(frames)

    monkeypatch.setattr(product_check, "label_frames", labels)
    return state


@pytest.fixture
def shoes(monkeypatch, fake_gen, fidelity):
    """Shoes route: mocked references, one video, a mocked turn check.

    The rotation and glitch checks stay guarded (conftest): neither may run
    on a shoe video.
    """
    _classified(fake_gen, "shoes", [("right", 1), ("other", 1), ("other", 2)])
    fake_gen.video(CLIP)
    state = {
        "refs": {
            "right": SIDE,
            "front_right": PAIR,
            "back_right": BACK,
            "top_front": TOP,
        },
        "mirrored": set(),
        "problems": [],
        "turned": None,  # the clip check_turn returns (None: unchanged)
    }
    monkeypatch.setattr(
        shoe_refs,
        "build",
        lambda images: shoe_refs.ShoeRefs(
            dict(state["refs"]), frozenset(state["mirrored"])
        ),
    )

    def turn(clip):
        details = {"degrees": 360, "findings": list(state["problems"])}
        return shoe_check.Turn(
            state["turned"] or clip,
            TURN_FRAMES,
            TURN_LABELS,
            list(state["problems"]),
            details,
        )

    monkeypatch.setattr(shoe_check, "check_turn", turn)
    return state


@pytest.fixture
def product(monkeypatch, fake_gen, fidelity, no_media):
    """Generic route with mocked rotation/glitch checks (see ``_staged``)."""
    _classified(fake_gen, "other", [("front", 1), ("back", 1)])
    return _staged(monkeypatch, fake_gen)


@pytest.fixture
def judged_first(monkeypatch, product, fidelity):
    """Each glitch call answers only after one judge call has ended.

    On a glitch rejection the spin does not wait for its judge
    (``product_check.evaluate``): the judge thread may not have run yet
    when the run ends, and may then call the next test's fake. A glitch
    call that never sees its judge fails the variation.
    """
    judged = threading.Semaphore(0)
    judge, glitches = (
        video_fidelity.evaluate_video_fidelity,
        motion.detect_glitches,
    )

    def recorded(*args, **kwargs):
        try:
            return judge(*args, **kwargs)
        finally:
            judged.release()

    def after_the_judge(video):
        assert judged.acquire(timeout=5), (
            "no judge call before the glitch answer"
        )
        return glitches(video)

    monkeypatch.setattr(video_fidelity, "evaluate_video_fidelity", recorded)
    monkeypatch.setattr(motion, "detect_glitches", after_the_judge)


# ---------------------------------------------------------------- shoes


async def test_shoes_route_events_and_checks(shoes, fidelity, fake_gen, images):
    events = await _collect(SpinRequest(images))
    assert _types(events) == [
        "classified",
        "progress",
        "progress",
        "variation",
        "variation",
        "variation",
        "complete",
    ]
    assert events[0].payload["product_type"] == "shoes"
    assert [(e.message, e.data) for e in events[1:3]] == [
        (
            "Using 4 reference views · 16:9 (auto)",
            {
                "views": list(shoes["refs"]),
                "aspect_ratio": "16:9",
                "closeups": [],
            },
        ),
        ("Generating spin videos", {}),
    ]
    call = fidelity.calls[0]
    assert (call["orientation"], call["judge"]) == ("shoe", "shoe")
    assert call["video"] == CLIP
    # The judge gets the frames the turn check labelled, with their labels.
    assert (call["frames"], call["labels"]) == (TURN_FRAMES, TURN_LABELS)
    # Top views are never judged (an eye-level spin never shows them).
    assert list(call["references"]) == ["right", "front_right", "back_right"]
    variations = [e for e in events if e.type == "variation"]
    assert {v.status for v in variations} == {"ready"}
    assert all(v.media == SPIN for v in variations)
    assert media.sniff_mime(SPIN) == "image/webp"
    assert core_video.probe(SPIN)[0] == core_video.probe(CLIP)[0]
    assert variations[0].verdict.details["turn"]["degrees"] == 360
    assert events[-1].best_index is not None


async def test_shoes_prompt_and_references_reach_the_video_model(
    shoes, fake_gen, images
):
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    [call] = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert call["raw_parts"] == [SIDE, PAIR, BACK, TOP]
    assert "toe of the shoe points to the RIGHT" in call["prompt"]
    assert call["prompt"] == shoe_prompt("16:9")  # nothing mirrored
    assert "mirror image" not in call["prompt"]
    assert call["params"] == VideoParams(aspect_ratio="16:9")
    assert not call["store"] and call["extends"] is None  # one request


async def test_mirrored_shoe_references_are_named_and_never_judged(
    shoes, fidelity, fake_gen, images
):
    shoes["refs"] = {"right": SIDE, "front_right": PAIR, "left": BACK}
    shoes["refs"]["front"] = TOP
    shoes["mirrored"] = {"front_right", "left"}
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    [call] = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert call["raw_parts"] == [SIDE, PAIR, BACK, TOP]  # still sent
    assert call["prompt"].startswith(shoe_prompt("16:9"))
    assert call["prompt"].endswith(
        "\n\nReference images 2 and 3 are generated mirror images of the "
        "other shoe of the pair: use them only for the shoe's shape, colour "
        "and materials; take text and logos only from the other references."
    )
    assert list(fidelity.calls[0]["references"]) == ["right", "front"]


async def test_a_shoe_spin_that_turns_back_is_rejected_without_judging(
    shoes, fidelity, images
):
    shoes["problems"] = ["turns back at 3.0 s"]
    events = await _collect(SpinRequest(images))
    variations = [e for e in events if e.type == "variation"]
    assert {v.status for v in variations} == {"rejected"}
    assert {v.verdict.description for v in variations} == {
        "turns back at 3.0 s"
    }
    assert fidelity.calls == []
    assert events[-1].best_index is None


async def test_the_shoe_clip_is_the_one_the_turn_check_returns(
    shoes, fidelity, images
):
    shoes["turned"] = REVERSED  # reversed and/or cut
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    assert fidelity.calls[0]["video"] == REVERSED
    # Already an animated WebP: not encoded again.
    assert [e.media for e in events if e.type == "variation"] == [REVERSED]


async def test_shoes_without_fidelity_views_publish_a_valid_spin(
    shoes, fidelity, images
):
    shoes["refs"] = {"left": PAIR, "top_front": TOP}  # never judged
    events = await _collect(SpinRequest(images))
    assert {e.status for e in events if e.type == "variation"} == {"ready"}
    assert fidelity.calls == []


async def test_no_shoe_reference_is_an_error(shoes, fake_gen, images):
    shoes["refs"] = {}
    events = await _collect(SpinRequest(images))
    assert _types(events) == ["classified", "error"]
    assert "no usable shoe reference" in events[-1].message
    assert not [c for c in fake_gen.calls if c["fn"] == "generate_video"]


# ---------------------------------------------------------------- other


async def test_other_route_references_and_fidelity(
    product, fidelity, fake_gen, no_media
):
    photos = [png(color=(i, 0, 0)) for i in range(1, 6)]
    _classified(
        fake_gen,
        "other",
        [("front", 2), ("front", 1), ("right", 1), ("other", 1), ("back", 1)],
    )
    events = await _collect(SpinRequest(photos))
    call = next(c for c in fake_gen.calls if c["fn"] == "generate_video")
    assert call["raw_parts"] == [photos[1], photos[2], photos[4]]
    assert "a black smartphone" in call["prompt"]
    # The three references are cut out in parallel: compare without order.
    assert sorted(no_media["one_item"]) == sorted(
        [(photos[1], None), (photos[2], None), (photos[4], None)]
    )
    fid = fidelity.calls[0]
    assert (fid["orientation"], fid["judge"]) == ("gemini", "product")
    assert list(fid["references"]) == ["front", "right", "back"]
    variation = next(e for e in events if e.type == "variation")
    assert variation.status == "ready"
    rotation = {"verdict": "clockwise", "reversed": False}  # no UI finding
    assert variation.verdict.details["rotation"] == rotation


async def test_other_route_without_main_views_uses_the_best_image(
    product, fidelity, fake_gen
):
    photos = [png(color=(i, 0, 0)) for i in range(1, 3)]
    _classified(fake_gen, "other", [("other", 2), ("other", 1)])
    events = await _collect(SpinRequest(photos))
    call = next(c for c in fake_gen.calls if c["fn"] == "generate_video")
    assert call["raw_parts"] == [photos[1]]
    assert fidelity.calls == []
    assert {e.status for e in events if e.type == "variation"} == {"ready"}


async def test_anticlockwise_is_reversed_and_emitted(
    product, fidelity, monkeypatch, images
):
    product["rotation"] = "anticlockwise"
    monkeypatch.setattr(core_video, "reverse", lambda clip: REVERSED)
    events = await _collect(SpinRequest(images))
    variations = [e for e in events if e.type == "variation"]
    assert all(v.media == REVERSED for v in variations)
    assert product["glitches"][0] == CLIP  # on the clip as made, at once
    assert fidelity.calls[0]["video"] == REVERSED
    assert {v.status for v in variations} == {"ready"}
    assert variations[0].verdict.details["rotation"] == {
        "verdict": "anticlockwise",
        "reversed": True,
        "findings": ["rotation check: anticlockwise, clip reversed"],
    }


async def test_debug_trace_saves_references_and_the_reversed_clip(
    product, monkeypatch, images, debug_dir
):
    product["rotation"] = "anticlockwise"
    monkeypatch.setattr(core_video, "reverse", lambda clip: REVERSED)
    with tracing() as root:
        await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    assert trace_file(root, "_ref-front.png").read_bytes() == images[0]
    reversed_clip = trace_file(root, "_v0a1_video_clip-reversed.webp")
    assert reversed_clip.read_bytes() == REVERSED
    verdict = json.loads(
        trace_file(root, "_v0a1_video_verdict.json").read_text()
    )
    assert verdict["status"] == "ready"


@pytest.mark.parametrize(
    ("glitch", "fidelity_ok", "status"),
    [
        (Verdict(True), True, "ready"),
        (Verdict(False, description="zooms out at 1 s"), True, "rejected"),
        (Verdict(True), False, "rejected"),
    ],
)
async def test_unknown_rotation_keeps_the_clip_for_glitch_and_fidelity(
    product,
    fidelity,
    judged_first,
    images,
    monkeypatch,
    glitch,
    fidelity_ok,
    status,
):
    monkeypatch.setattr(spin_pipeline, "CONTINUATIONS", 1)  # one spin
    product["rotation"] = "unknown"
    product["glitch"] = glitch
    if not fidelity_ok:
        fidelity.result["verdict"] = Verdict(False, 1, "logo differs")
    monkeypatch.setattr(core_video, "reverse", lambda clip: pytest.fail())
    policy = RunPolicy(num_variations=1)
    events = await _collect(SpinRequest(images, policy=policy))
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == status
    assert variation.media == SPIN  # the clip as made, as a WebP
    assert product["glitches"] == [CLIP]
    # the judge runs while the glitch call ends; a glitch still rejects
    assert [c["video"] for c in fidelity.calls] == [CLIP]
    assert variation.verdict.details["rotation"] == {
        "verdict": "unknown",
        "reversed": False,
        "findings": [
            "rotation check: too little motion to tell, clip kept as is"
        ],
    }


async def test_a_spin_that_turns_back_is_rejected_before_the_other_checks(
    product, fidelity, images
):
    product["rotation"] = "invalid"
    policy = RunPolicy(num_variations=1)
    events = await _collect(SpinRequest(images, policy=policy))
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == "rejected"
    assert variation.score == quality_score(False, 2) == 40.0
    assert variation.verdict.description.startswith("the spin turns back")
    assert variation.verdict.details["rotation"]["findings"] == [
        "rotation check: the spin turns back"
    ]
    assert fidelity.calls == []


def test_the_judge_ignores_removable_parts_the_spin_leaves_off():
    assert "removable accessory" in video_fidelity.PRODUCT_JUDGE_SYSTEM
    assert "not a difference" in video_fidelity.PRODUCT_JUDGE_SYSTEM


async def test_a_glitch_rejects_even_when_the_judge_passes(
    product, fidelity, judged_first, images
):
    product["glitch"] = Verdict(
        False, description="the phone shows two screens"
    )
    events = await _collect(SpinRequest(images))
    variation = next(e for e in events if e.type == "variation")
    assert variation.status == "rejected"
    assert variation.verdict.description == "the phone shows two screens"
    assert variation.verdict.imperfection_count == 2
    assert variation.score == quality_score(False, 2) == 40.0
    assert fidelity.calls  # judged meanwhile; the glitch verdict wins


@pytest.mark.parametrize("defects", [0, 1, 2])
async def test_glitch_reject_never_outranks_a_fidelity_reject(
    product, fidelity, images, monkeypatch, defects
):
    monkeypatch.setattr(spin_pipeline, "CONTINUATIONS", 1)  # one spin each
    answers = iter([Verdict(False, description="cuts at 1 s"), Verdict(True)])
    monkeypatch.setattr(
        motion,
        "detect_glitches",
        lambda video: Verdict(True) if video == HALF else next(answers),
    )
    fidelity.result["verdict"] = Verdict(False, defects, "logo differs")
    policy = RunPolicy(mode="sequential", max_attempts=2)
    events = await _collect(SpinRequest(images, policy=policy))
    glitched, judged = [e for e in events if e.type == "variation"]
    assert glitched.verdict.description == "cuts at 1 s"
    assert judged.verdict.description == "logo differs"
    assert glitched.score <= judged.score
    if defects < 2:  # a 2-defect fidelity reject ties with a glitch reject
        assert events[-1].ranked_indices == [judged.index, glitched.index]


async def test_glitch_eval_error_marks_the_variation_error(product, images):
    product["glitch"] = Verdict(
        False, description="eval incomplete", error=True
    )
    events = await _collect(SpinRequest(images))
    variations = [e for e in events if e.type == "variation"]
    assert {v.status for v in variations} == {"error"}
    assert {v.verdict.imperfection_count for v in variations} == {0}


async def test_fidelity_reject(product, fidelity, images):
    fidelity.result["verdict"] = Verdict(False, 2, "logo differs")
    events = await _collect(SpinRequest(images))
    variation = next(e for e in events if e.type == "variation")
    assert (variation.status, variation.verdict.imperfection_count) == (
        "rejected",
        2,
    )
    assert variation.score == quality_score(False, 2) == 40.0
    assert variation.verdict.details["score"] == variation.score


async def test_shoes_score_is_quality_score(shoes, images):
    events = await _collect(SpinRequest(images))
    variation = next(e for e in events if e.type == "variation")
    assert variation.score == quality_score(True, 0) == 100.0


# ---------------------------------------------------------------- common


async def test_a_product_spin_is_a_first_half_then_its_continuation(
    product, fake_gen, images
):
    params = VideoParams(aspect_ratio="9:16", resolution="1080p", deadline_s=60)
    policy = RunPolicy(num_variations=1)
    events = await _collect(SpinRequest(images, policy=policy, video=params))
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    first, second, spare = calls  # the spare continuation starts at once
    assert spare["extends"] == second["extends"] == "turn-1.1"
    half = dataclasses.replace(params, duration="4s")  # each half turn 4 s
    assert first["params"] == second["params"] == half
    assert first["store"] is True and first["extends"] is None
    assert first["prompt"].startswith(
        "[duration=4s] Smooth continuous camera orbit"
    )
    assert second["extends"] == "turn-1.1"  # continues the stored first half
    assert second["prompt"].startswith("[duration=4s] Continue the smooth")
    assert second["raw_parts"] == first["raw_parts"]  # the same references
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == "ready" and variation.media == SPIN


async def test_a_first_half_that_turns_back_is_made_again_before_continuing(
    product, fake_gen, images
):
    product["half_rotation"] = ["invalid"]
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert [c["store"] for c in calls] == [True, True, False, False]
    assert calls[2]["extends"] == "turn-1.2"  # the second, good first half


async def test_a_first_half_failing_twice_is_continued_anyway(
    product, fake_gen, images
):
    product["half_rotation"] = ["invalid", "invalid"]
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert [c["store"] for c in calls] == [True, True, False, False]


async def test_a_rejected_spin_is_continued_again_from_its_first_half(
    product, fake_gen, images, monkeypatch
):
    verdicts = iter([Verdict(False, 2, "logo missing"), Verdict(True)])
    monkeypatch.setattr(
        video_fidelity,
        "evaluate_video_fidelity",
        lambda video, references, **kw: next(verdicts),
    )
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    # One first half, continued twice: the first 4 s are kept.
    assert [(c["store"], c["extends"]) for c in calls] == [
        (True, None),
        (False, "turn-1.1"),
        (False, "turn-1.1"),
    ]
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == "ready"


async def test_the_spare_continuation_starts_before_the_verdict(
    product, fake_gen, images, monkeypatch
):
    import threading

    spare_started = threading.Event()
    real = spin_pipeline._evaluate

    def judged_once_spare_started(clip, refs, route):
        assert spare_started.wait(timeout=5), "the spare waited for the check"
        return real(clip, refs, route)

    video = fake_gen._responders["video"][0]  # the staged half turns

    def tracked(images, prompt, *, params=None, store=False, extends=None):
        out = video(images, prompt, params=params, store=store, extends=extends)
        continuations = [c for c in fake_gen.calls if c.get("extends")]
        if len(continuations) == 2:  # the continuation and the spare
            spare_started.set()
        return out

    fake_gen.video(tracked)
    monkeypatch.setattr(spin_pipeline, "_evaluate", judged_once_spare_started)
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == "ready"


async def test_continuations_stop_after_the_limit_and_keep_the_best(
    product, fake_gen, images, fidelity
):
    fidelity.result["verdict"] = Verdict(False, 1, "logo differs")
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    assert len(calls) == 1 + spin_pipeline.CONTINUATIONS
    [variation] = [e for e in events if e.type == "variation"]
    assert variation.status == "rejected"


async def test_three_parallel_variations_by_default(product, fake_gen, images):
    events = await _collect(SpinRequest(images))
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    # each: a first half, its continuation and the spare one
    assert sorted(c["store"] for c in calls) == [False] * 6 + [True] * 3
    assert sorted(e.index for e in events if e.type == "variation") == [0, 1, 2]
    assert sorted(events[-1].ranked_indices) == [0, 1, 2]


async def test_sequential_policy_stops_at_the_first_ready(
    product, fake_gen, images
):
    policy = RunPolicy(mode="sequential", max_attempts=5)
    events = await _collect(SpinRequest(images, policy=policy))
    assert _types(events)[-2:] == ["variation", "complete"]
    # One spin: its first half, the continuation and the spare one.
    assert len([c for c in fake_gen.calls if c["fn"] == "generate_video"]) == 3


async def test_closing_the_stream_closes_the_runner_at_once(
    product, images, monkeypatch
):
    closed: list[bool] = []
    real = spin_pipeline.run_variations

    async def tracked(*args):
        try:
            async for event in real(*args):
                yield event
        finally:
            closed.append(True)

    monkeypatch.setattr(spin_pipeline, "run_variations", tracked)
    stream = run_spin(SpinRequest(images))
    events = [await anext(stream) for _ in range(4)]
    await stream.aclose()
    assert _types(events) == ["classified", "progress", "progress", "variation"]
    assert closed == [True]


async def test_failed_generation_is_an_error_variation(
    product, fake_gen, images
):
    fake_gen.video([GenerationError("blocked"), CLIP, CLIP])
    events = await _collect(SpinRequest(images))
    statuses = sorted(e.status for e in events if e.type == "variation")
    assert statuses == ["error", "ready", "ready"]
    assert events[-1].type == "complete"


@pytest.mark.parametrize("count", [0, 9])
async def test_wrong_image_count_is_an_error_event(fake_gen, count):
    events = await _collect(SpinRequest([png()] * count))
    assert _types(events) == ["error"]
    assert events[0].retryable is False
    assert fake_gen.calls == []


async def test_empty_image_is_an_error_event(fake_gen):
    events = await _collect(SpinRequest([png(), b""]))
    assert _types(events) == ["error"]


async def test_classifier_failure_is_a_retryable_error_event(fake_gen, images):
    fake_gen.json(GenerationError("model overloaded", retryable=True))
    events = await _collect(SpinRequest(images))
    assert _types(events) == ["error"]
    assert events[0].retryable is True
    assert "model overloaded" in events[0].message


async def test_an_unexpected_failure_hides_its_text(fake_gen, images, caplog):
    fake_gen.json(RuntimeError("could not read gs://secret/creds.json"))
    events = await _collect(SpinRequest(images))
    assert _types(events) == ["error"]
    assert events[0].message == SERVER_FAILED
    assert "gs://secret/creds.json" in caplog.text  # in the log only


def _turning_clip(period: int, frames: int = 120) -> bytes:
    """A texture sliding 2 px a frame that repeats every ``period`` frames."""
    rng = np.random.default_rng(1)
    texture = rng.integers(0, 255, size=(64, 2 * period + 64), dtype=np.uint8)
    pngs = []
    for i in range(frames):
        x = 2 * (i % period)
        buf = io.BytesIO()
        Image.fromarray(texture[:, x : x + 64]).convert("RGB").save(buf, "PNG")
        pngs.append(buf.getvalue())
    return core_video.encode_frames(pngs, fps=24)


def test_a_spin_past_360_is_cut_back_to_its_start():
    clip = _turning_clip(period=110)  # back at the start at frame 110 of 120
    closed, cut = product_check._close_loop(clip, 1.0)
    assert cut == 111
    assert core_video.probe(closed)[0] == 111


def test_a_spin_ending_on_its_start_is_kept():
    clip = _turning_clip(period=119)  # the last frame is the return
    closed, cut = product_check._close_loop(clip, 1.0)
    assert cut is None and closed is clip


def test_a_short_clip_is_never_cut():
    clip = _turning_clip(period=40, frames=48)  # 2 s: not a full spin
    assert product_check._close_loop(clip, 1.0) == (clip, None)


def test_a_shoe_spin_overshooting_by_2_5_s_is_cut_with_its_3_s_window():
    clip = _turning_clip(period=180, frames=240)  # back at the start at 7.5 s
    closed, cut = product_check._close_loop(clip, 3.0)
    assert cut == 181
    assert core_video.probe(closed)[0] == 181


def test_a_clip_under_three_windows_is_never_cut():
    # 8 s with a 3 s window: the search would reach the half turn.
    clip = _turning_clip(period=150, frames=192)
    assert product_check._close_loop(clip, 3.0) == (clip, None)


# ---------------------------------------------------------------- aspect


def _video_params(fake_gen) -> VideoParams:
    return next(c for c in fake_gen.calls if c["fn"] == "generate_video")[
        "params"
    ]


@pytest.mark.parametrize(
    ("size", "ratio"), [((100, 300), "9:16"), ((300, 100), "16:9")]
)
async def test_auto_aspect_follows_the_product_shape(
    product, fake_gen, size, ratio
):
    photos = [png(*size, color=(i, 0, 0)) for i in (1, 2)]
    events = await _collect(
        SpinRequest(photos, policy=RunPolicy(num_variations=1))
    )
    assert _video_params(fake_gen).aspect_ratio == ratio
    refs = next(e for e in events if getattr(e, "stage", "") == "references")
    assert refs.message.endswith(f"· {ratio} (auto)")
    assert refs.data["aspect_ratio"] == ratio


async def test_a_near_square_product_stays_16_9(product, fake_gen):
    photos = [png(83, 100), png(60, 100)]  # the briefcase: widest 0.83
    await _collect(SpinRequest(photos, policy=RunPolicy(num_variations=1)))
    assert _video_params(fake_gen).aspect_ratio == "16:9"


async def test_a_chosen_aspect_ratio_is_kept(product, fake_gen):
    photos = [png(100, 300), png(90, 300)]
    params = VideoParams(aspect_ratio="16:9")
    events = await _collect(
        SpinRequest(photos, policy=RunPolicy(num_variations=1), video=params)
    )
    assert _video_params(fake_gen).aspect_ratio == "16:9"
    refs = next(e for e in events if getattr(e, "stage", "") == "references")
    assert "(auto)" not in refs.message


async def test_a_shoe_is_measured_on_its_side_view(shoes, fake_gen, images):
    shoes["refs"] = {"right": png(100, 300), "front": png(300, 100)}  # a boot
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    params = _video_params(fake_gen)
    assert params.aspect_ratio == "9:16"


@pytest.mark.parametrize(
    ("refs", "ratio"),
    [
        # No right view: the first side view (here a three-quarter view).
        ({"front": png(300, 100), "back_right": png(100, 300)}, "9:16"),
        ({"left": png(100, 300), "back_left": png(300, 100)}, "9:16"),
        ({"front": png(100, 300), "top_front": png(100, 300)}, "16:9"),
    ],
)
async def test_a_shoe_without_a_right_view_is_measured_on_a_side_view(
    shoes, fake_gen, images, refs, ratio
):
    shoes["refs"] = refs
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    assert _video_params(fake_gen).aspect_ratio == ratio


# ---------------------------------------------------------------- close-ups


def _closeup_setting(monkeypatch, on: bool):
    from genmedia4commerce.core import settings

    monkeypatch.setenv("GENMEDIA_SPIN_CLOSEUPS", "1" if on else "0")
    settings.get_settings.cache_clear()


async def test_closeups_go_after_the_views_and_into_the_prompt(
    product, fake_gen, fidelity, monkeypatch, images
):
    from genmedia4commerce.workflows import details

    _closeup_setting(monkeypatch, True)
    plaque = details.Detail("text", "Aurelia", "front", "plaque", b"PLAQUE")
    found = []
    monkeypatch.setattr(
        details,
        "find",
        lambda photos, limit: found.append(limit) or [plaque],
    )
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    assert found == [2]
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_video"]
    first, second, _spare = calls
    assert first["raw_parts"][-1] == b"PLAQUE" and len(first["raw_parts"]) == 3
    assert "<IMAGE_2> is a close-up of the plaque" in first["prompt"]
    assert "the lettering reading 'Aurelia'" in first["prompt"]
    assert (
        "matching <IMAGE_0>, with the plaque exactly as in <IMAGE_2>"
        in (second["prompt"])
    )
    assert list(fidelity.calls[0]["references"]) == ["front", "back"]  # views
    refs = next(e for e in events if getattr(e, "stage", "") == "references")
    assert "+ 1 close-ups" in refs.message
    assert refs.data["closeups"] == ["plaque"]


async def test_closeups_are_found_while_the_references_are_built(
    product, fake_gen, fidelity, monkeypatch, images
):
    import threading

    from genmedia4commerce.workflows import details
    from genmedia4commerce.workflows.spin import product_refs

    _closeup_setting(monkeypatch, True)
    searching = threading.Event()
    plaque = details.Detail("text", "Aurelia", "front", "plaque", b"PLAQUE")

    def find(photos, limit):
        searching.set()
        return [plaque]

    build = product_refs.build

    def refs_once_searching(*args, **kwargs):
        assert searching.wait(timeout=5), "the search waited for the references"
        return build(*args, **kwargs)

    monkeypatch.setattr(details, "find", find)
    monkeypatch.setattr(product_refs, "build", refs_once_searching)
    events = await _collect(
        SpinRequest(images, policy=RunPolicy(num_variations=1))
    )
    refs = next(e for e in events if getattr(e, "stage", "") == "references")
    assert refs.data["closeups"] == ["plaque"]


async def test_closeups_are_off_by_default(
    product, fake_gen, monkeypatch, images
):
    from genmedia4commerce.workflows import details

    _closeup_setting(monkeypatch, False)
    monkeypatch.setattr(details, "find", lambda *a, **k: pytest.fail("called"))
    await _collect(SpinRequest(images, policy=RunPolicy(num_variations=1)))
    first = next(c for c in fake_gen.calls if c["fn"] == "generate_video")
    assert "close-up" not in first["prompt"]
