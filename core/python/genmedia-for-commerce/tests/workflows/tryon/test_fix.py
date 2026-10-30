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

"""Tests for the try-on fix pass (``GENMEDIA_TRYON_FIX``): image and judge
calls faked."""

import asyncio

import pytest

from genmedia4commerce.core import gen
from genmedia4commerce.core.errors import GenerationError, user_message
from genmedia4commerce.core.events import VariationEvent
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.retry import Deadline
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Product,
    ProductRef,
    Verdict,
)
from genmedia4commerce.eval import outfit
from genmedia4commerce.workflows.tryon import fix_prompt, pipeline, prompt
from genmedia4commerce.workflows.tryon.types import TryOnContext
from tests.conftest import trace_lines, tracing

PERSON = png(96, 128, color=(230, 200, 180))
FACE = png(16, 16, color=(1, 2, 3))
FRONT = png(12, 16, color=(9, 9, 9))
DRAWN = png(12, 16, color=(100, 0, 0))
FIXED = png(12, 16, color=(0, 100, 0))
LEGS = "the legs are darker than the face and hands"
KEEP_ADAPT = (
    "Everything else stays as in CURRENT RESULT (pose, framing, background, "
    "light, every other product and everything else the wearer has on, "
    "accessories included), except what touches a corrected part, which "
    "adapts so the photo stays natural (the hair and a hat around a "
    "corrected face, the fabric around a corrected hem)."
)
SLEEVES = {
    "feature": "sleeve length",
    "category": "shape",
    "kind": "different",
    "explanation": "the sleeves end at the elbow, the product's at the wrist",
}


def _product(pid, category, color, view="front"):
    ref = ProductRef(png(color=color), view)
    return Product(pid, category, f"{category} item", [ref])


def _ctx(*products, face=FACE):
    mode = "single" if len(products) == 1 else "outfit"
    c = Classification(mode, list(products), "full_body")
    return TryOnContext(
        classification=c,
        wearer_body=PERSON,
        wearer_face=face,
        reference_face=face,
        style_guidelines="",
        scenario="a plain studio",
        params=ImageParams(image_size="1K"),
        chosen=[(p, p.refs[0]) for p in products],
    )


def _rejected(body=(), products=None, description="rejected", parts=()):
    """A judge answer as ``evaluate_outfit`` returns it: products by
    1-based index; ``parts``: the body parts (none in an old verdict)."""
    entries = [
        {"index": k, "defects": d, "findings": []}
        for k, d in (products or {}).items()
    ]
    details = {"products": entries, "body_defects": list(body)}
    return Verdict(
        publish=False,
        imperfection_count=1,
        description=description,
        details=details | ({"body_parts": list(parts)} if parts else {}),
    )


@pytest.fixture
def judge(monkeypatch):
    """The outfit judge: answers ``verdicts`` in turn (the last repeats);
    ``targets`` holds each judged image."""
    state = {"verdicts": [Verdict(publish=True)], "targets": []}

    def evaluate_outfit(products, target, *, wearer=None, face=None):
        state["targets"].append(target)
        queue = state["verdicts"]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)
    return state


@pytest.fixture
def fix_on(monkeypatch):
    monkeypatch.setenv("GENMEDIA_TRYON_FIX", "true")
    get_settings.cache_clear()


def _run(ctx, view="front", front=None) -> VariationEvent:
    async def collect():
        policy = RunPolicy(num_variations=1)
        events = pipeline._variations(ctx, policy, Deadline(60), view, front)
        return [e async for e in events]

    return next(e for e in asyncio.run(collect()) if e.type == "variation")


def _calls(fake_gen) -> list[dict]:
    return [c for c in fake_gen.calls if c["fn"] == "generate_image"]


def _images(call) -> list[bytes]:
    return [p for p in call["raw_parts"] if not isinstance(p, str)]


def test_flag_off_makes_no_extra_call(fake_gen, judge):
    judge["verdicts"] = [_rejected(body=[LEGS])]
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 1 and judge["targets"] == [DRAWN]
    assert event.media == DRAWN and event.status == "rejected"
    assert "fix" not in event.verdict.details and event.fixed is False
    assert event.to_dict()["fixed"] is False


def test_a_body_defect_sends_the_person_and_the_result_only(
    fake_gen, judge, fix_on
):
    judge["verdicts"] = [_rejected(body=[LEGS]), Verdict(publish=True)]
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    drawing, fixing = _calls(fake_gen)
    assert fixing["params"] == drawing["params"] == ImageParams(image_size="1K")
    assert fixing["raw_parts"][:-1] == [
        f"{prompt._INPUTS}\n- image_0.png — {prompt._WEARER_BODY}",
        PERSON,
        f"- image_1.png — {prompt._WEARER_FACE}",
        FACE,
        f"- image_2.png — {fix_prompt.CURRENT}",
        DRAWN,
    ]
    text = fixing["raw_parts"][-1]
    assert (
        f"- {LEGS} — follow image_0.png (WEARER BODY) and image_1.png "
        "(WEARER FACE)." in text
    )
    assert "each as the image named next to it shows" in text
    assert KEEP_ADAPT in text and "exactly as in CURRENT" not in text
    assert "taking the body" not in text
    assert "named product" not in text
    assert "how each listed defect is corrected and which image" in text
    system = fixing["system"]
    assert (
        "You receive 3 labelled reference images (image_0.png through "
        in system
    )
    assert (
        "all 3 input filenames in ascending order (image_0.png, image_1.png, "
        "image_2.png)" in system
    )
    assert "skin tone, shade, undertone" in system and "image_0.png" in system
    # Judged again; the fixed image and its verdict are the variation's.
    assert judge["targets"] == [DRAWN, FIXED]
    assert event.media == FIXED and event.status == "ready"
    assert event.verdict.details["fixed"] is True and event.fixed is True
    assert event.to_dict()["fixed"] is True


def test_a_product_defect_sends_only_that_products_image(
    fake_gen, judge, fix_on
):
    top, bottom = (
        _product("A", "top", (0, 0, 200)),
        _product("B", "bottom", (0, 200, 0)),
    )
    judge["verdicts"] = [_rejected(products={1: [SLEEVES]})]
    fake_gen.image([DRAWN, FIXED])
    _run(_ctx(top, bottom))
    drawing, fixing = _calls(fake_gen)
    sent = _images(drawing)  # body, face, A's image, B's image
    assert _images(fixing) == [sent[0], sent[1], DRAWN, sent[2]]
    # A's drawing label, renamed: image_2.png there, image_3.png here.
    drawn_label = drawing["raw_parts"][drawing["raw_parts"].index(sent[2]) - 1]
    label = fixing["raw_parts"][fixing["raw_parts"].index(sent[2]) - 1]
    assert label == drawn_label.replace("image_2.png", "image_3.png")
    assert label.startswith("- image_3.png — PRODUCT A (top item)")
    text = fixing["raw_parts"][-1]
    assert (
        "- PRODUCT A (top item), sleeve length — follow image_3.png "
        "(PRODUCT A top item) and its line above." in text
    )
    assert SLEEVES["explanation"] not in text
    assert (
        "Images: image_0.png: WEARER BODY; image_1.png: WEARER FACE; " in text
    )
    assert "image_3.png: PRODUCT A top item." in text
    assert "each as the image named next to it shows" in text
    assert "(image_0.png through image_3.png)" in fixing["system"]


def test_a_body_defect_is_named_by_its_body_part(fake_gen, judge, fix_on):
    judge["verdicts"] = [_rejected(body=[LEGS], parts=["lower legs"])]
    fake_gen.image([DRAWN, FIXED])
    _run(_ctx(_product("A", "top", (0, 0, 200))))
    text = _calls(fake_gen)[1]["raw_parts"][-1]
    assert (
        "- Body, lower legs — follow image_0.png (WEARER BODY) and "
        "image_1.png (WEARER FACE)." in text
    )
    assert LEGS not in text and KEEP_ADAPT in text


def test_a_defect_without_a_short_name_keeps_the_judges_words():
    """A defect without a short name (an old verdict: no body part, no
    feature) keeps the judge's long text."""
    ctx = _ctx(_product("A", "top", (0, 0, 200)))
    spec = pipeline._spec(ctx, "front")
    parts = [
        f"{prompt._INPUTS}\n- image_0.png — x",
        PERSON,
        "- image_1.png — y",
        FACE,
        "- image_2.png — PRODUCT A (top item).",
        b"A",
        "rules",
    ]
    unnamed = {**SLEEVES, "feature": ""}
    verdict = _rejected(body=[LEGS, "the face"], products={1: [unnamed]})
    verdict.details["products"][0]["product_id"] = "A"
    verdict.details["body_parts"] = ["", "face"]
    _, request = fix_prompt.build_prompt(spec, parts, DRAWN, verdict)
    assert request[-1].split("defects:\n")[1].splitlines()[:3] == [
        f"- {LEGS} — follow image_0.png (WEARER BODY) and image_1.png "
        "(WEARER FACE).",
        "- Body, face — follow image_0.png (WEARER BODY) and image_1.png "
        "(WEARER FACE).",
        f"- PRODUCT A (top item), {SLEEVES['explanation']} — follow "
        "image_3.png (PRODUCT A top item) and its line above.",
    ]


def test_what_touches_a_correction_adapts():
    """The rest is kept, but not frozen around a corrected part."""
    assert KEEP_ADAPT in fix_prompt.CORRECTION
    assert "exactly" not in fix_prompt.CORRECTION + fix_prompt.CURRENT
    assert fix_prompt.CURRENT.endswith("except what touches a corrected part.")


def test_other_images_a_label_names_are_renamed():
    """A product label naming another product's image gets the product's
    name when that image is not sent."""
    ctx = _ctx(
        _product("A", "top", (0, 0, 200)), _product("B", "bottom", (0, 9, 0))
    )
    spec = pipeline._spec(ctx, "front")
    parts = [
        f"{prompt._INPUTS}\n- image_0.png — WEARER BODY: x",
        PERSON,
        "- image_1.png — WEARER FACE: y",
        FACE,
        "- image_2.png — PRODUCT A (top item): under (image_3.png). "
        "In image_0.png.",
        b"A",
        "- image_3.png — PRODUCT B (bottom item).",
        b"B",
        "rules",
    ]
    verdict = _rejected(products={1: [SLEEVES]})
    verdict.details["products"][0]["product_id"] = "A"
    _, request = fix_prompt.build_prompt(spec, parts, DRAWN, verdict)
    assert request[6] == (
        "- image_3.png — PRODUCT A (top item): under "
        "(PRODUCT B bottom item). In image_0.png."
    )
    assert request[7] == b"A" and b"B" not in request


def test_a_rejected_fix_keeps_the_image(fake_gen, judge, fix_on):
    still = Verdict(publish=False, description="the legs are still darker")
    judge["verdicts"] = [_rejected(body=[LEGS]), still, Verdict(publish=True)]
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 2 and judge["targets"] == [DRAWN, FIXED]
    assert event.media == DRAWN and event.status == "rejected"
    assert event.verdict.details["fix"] == {
        "published": False,
        "description": "the legs are still darker",
    }
    assert "fixed" not in event.verdict.details and event.fixed is False


def test_a_published_image_is_not_fixed(fake_gen, judge, fix_on):
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 1 and event.status == "ready"
    assert event.fixed is False and "fix" not in event.verdict.details


def test_a_judge_error_is_not_fixed(fake_gen, judge, fix_on):
    judge["verdicts"] = [Verdict(publish=False, error=True, description="x")]
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 1 and event.status == "error"
    assert "fix" not in event.verdict.details


def test_over_the_image_limit_nothing_is_dropped(
    fake_gen, judge, fix_on, monkeypatch
):
    """Person, face, result and A's image are 4: over a limit of 3 the fix
    is skipped, never sent with fewer images."""
    monkeypatch.setattr(gen, "MAX_IMAGES", 3)
    judge["verdicts"] = [_rejected(products={1: [SLEEVES]})]
    fake_gen.image([DRAWN, FIXED])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 1 and judge["targets"] == [DRAWN]
    assert event.media == DRAWN
    assert event.verdict.details["fix"] == {"skipped": "image limit"}


def test_a_failed_fix_keeps_the_image(fake_gen, judge, fix_on):
    down = GenerationError("image model down")
    judge["verdicts"] = [_rejected(body=[LEGS])]
    fake_gen.image([DRAWN, down])
    event = _run(_ctx(_product("A", "top", (0, 0, 200))))
    assert len(_calls(fake_gen)) == 2 and judge["targets"] == [DRAWN]
    assert event.media == DRAWN and event.status == "rejected"
    assert event.verdict.details["fix"] == {"error": user_message(down)}


def test_the_back_view_takes_the_runs_person(fake_gen, judge, fix_on):
    """The back view is drawn from FRONT RESULT alone: the fix gets the
    run's person and face, and the product's images without FRONT
    RESULT."""
    shoes = _product("A", "footwear", (90, 60, 30), view="back")
    judge["verdicts"] = [_rejected(products={1: [SLEEVES]})]
    fake_gen.image([DRAWN, FIXED])
    _run(_ctx(shoes), view="back", front=FRONT)
    drawing, fixing = _calls(fake_gen)
    assert _images(drawing)[-1] == FRONT
    assert _images(fixing) == [PERSON, FACE, DRAWN, shoes.refs[0].image]


def test_without_a_face_crop_the_body_stands_for_it(fake_gen, judge, fix_on):
    judge["verdicts"] = [_rejected(body=[LEGS])]
    fake_gen.image([DRAWN, FIXED])
    _run(_ctx(_product("A", "top", (0, 0, 200)), face=None))
    _, fixing = _calls(fake_gen)
    assert _images(fixing) == [PERSON, DRAWN]
    text = fixing["raw_parts"][-1]
    assert f"- {LEGS} — follow image_0.png (WEARER BODY)." in text


def test_no_listed_defect_falls_back_to_the_description(
    fake_gen, judge, fix_on
):
    judge["verdicts"] = [_rejected(description="The colour is off.")]
    fake_gen.image([DRAWN, FIXED])
    _run(_ctx(_product("A", "top", (0, 0, 200))))
    _, fixing = _calls(fake_gen)
    assert "defects:\n- The colour is off.\n" in fixing["raw_parts"][-1]


def test_the_fix_is_traced(fake_gen, judge, fix_on, debug_dir):
    """With a debug trace the fix's calls carry the label ``fix`` and its
    verdict is saved as ``fix``."""
    judge["verdicts"] = [_rejected(body=[LEGS]), Verdict(publish=True)]
    fake_gen.image([DRAWN, FIXED])
    with tracing() as root:
        _run(_ctx(_product("A", "top", (0, 0, 200))))
    (line,) = [e for e in trace_lines(root) if e["label"] == "fix"]
    assert line["labels"]["fix"] == 1 and line["labels"]["view"] == "front"
    assert line["file"].endswith("_fix.json")
