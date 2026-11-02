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

"""Tests for the try-on pipeline (all model, media and eval calls faked)."""

import asyncio
import io
import threading
import time
from dataclasses import dataclass, field, replace

import pytest
from PIL import Image

from genmedia4commerce.core import media
from genmedia4commerce.core.errors import SERVER_FAILED, GenerationError
from genmedia4commerce.core.events import (
    ClassifiedEvent,
    CompleteEvent,
    ErrorEvent,
    ProgressEvent,
    RecordEvent,
    VariationEvent,
)
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Verdict, quality_score
from genmedia4commerce.eval import outfit
from genmedia4commerce.eval.garment_prompts import PRODUCT_FILTER_HINTS
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.tryon import (
    cards,
    closeups,
    cut,
    pipeline,
    record,
    reference,
)
from genmedia4commerce.workflows.tryon.cut import Boxed
from genmedia4commerce.workflows.tryon.prompt import FRAMING
from genmedia4commerce.workflows.tryon.types import Prepared, TryOnRequest

FACE_BOX = (2.0, 2.0, 6.0, 6.0)
FACE_CROP = png(16, 16, color=(1, 2, 3))
GENERATED = [png(12, 16, color=(100 + i, 0, 0)) for i in range(6)]


REAL_OUTFIT_JUDGE = outfit.evaluate_outfit


@dataclass
class Fakes:
    """Recorded calls to the faked media and eval functions."""

    product_calls: list[dict] = field(default_factory=list)
    judge_calls: int = 0  # outfit judge calls
    wearers: list = field(default_factory=list)  # each judge call's wearer
    faces: list = field(default_factory=list)  # each judge call's face crop
    crop_calls: list[bytes] = field(default_factory=list)  # photos boxed
    faces_cut: list[bytes] = field(default_factory=list)  # photos cut
    boxes: dict[str, bytes] = field(default_factory=dict)  # name: its crop
    asked: list = field(default_factory=list)  # (items, bands) per box call
    upscaled: list[bytes] = field(default_factory=list)
    found: dict[bytes, list] = field(default_factory=dict)  # close-ups
    searched: list[list[bytes]] = field(default_factory=list)  # searches
    told: list[str] = field(default_factory=list)  # each search's product
    face_box: tuple | None = FACE_BOX  # Cloud Vision's, of the photo
    publish: bool = True
    imperfections: int = 0


@pytest.fixture
def fakes(monkeypatch) -> Fakes:
    f = Fakes()
    monkeypatch.setattr(media, "face_box", lambda data: f.face_box)
    real_crop = media.crop_box

    def crop_box(data, box, **kw):
        """The face box's crop; the photo it is cut from is recorded."""
        if box != FACE_BOX:
            return real_crop(data, box, **kw)
        f.faces_cut.append(data)
        return FACE_CROP

    monkeypatch.setattr(media, "crop_box", crop_box)

    def evaluate_outfit(products, target, *, wearer=None, face=None):
        """One judge call; records one entry per product it is shown."""
        f.judge_calls += 1
        f.wearers.append(wearer)
        f.faces.append(face)
        f.product_calls += [
            {
                "references": [p.reference],
                "target": target,
                "product_filter": p.product_filter,
                "visibility": p.visibility,
            }
            for p in products
        ]
        return Verdict(
            publish=f.publish,
            imperfection_count=f.imperfections,
            description="ok",
        )

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)

    def box_items(image, items=(), *, bands=False):
        """One box call: the halves when asked, and the crop of each item
        named in ``boxes`` (None: not found)."""
        f.crop_calls.append(image)
        f.asked.append((list(items), bands))
        halves = {"top": CROPS[0], "bottom": CROPS[2]} if bands else {}
        crops = [f.boxes.get(name) for _, name in items]
        return Boxed(halves, crops, [None] * len(items))

    def upscale_shoes(crop):
        f.upscaled.append(crop)
        return SHOES_2K

    def find(images, *, limit=2, upscale=True, product="", anchor=None):
        """The detail search of a product's photos (its anchor first),
        told its name: what ``found`` lists for each photo (none by
        default), cropped from it (``photo``: its index), at most
        ``limit``."""
        assert (limit, upscale) == (closeups.SEARCHED, False)
        photos = list(images) if anchor is None else [anchor, *images]
        f.searched.append(photos)
        f.told.append(product)
        hits = [
            replace(d, photo=i)
            for i, image in enumerate(photos)
            for d in f.found.get(image, [])
        ]
        return hits[:limit]

    monkeypatch.setattr(reference, "box_items", box_items)
    monkeypatch.setattr(reference, "upscale_shoes", upscale_shoes)
    monkeypatch.setattr(details, "find", find)
    return f


CROPS = [png(10, 10, color=(0, 50 + i, 0)) for i in range(3)]
SHOES, SHOES_2K = (
    png(12, 6, color=(90, 60, 30)),
    png(24, 12, color=(91, 61, 31)),
)


def _img(index, view="front", presentation="flat", rank=1, **extra):
    return {
        "index": index,
        "view": view,
        "presentation": presentation,
        "rank": rank,
        **extra,
    }


# Where a garment of each category starts and ends, in the fake answers.
SPANS = {
    "top": ("shoulders", "hips"),
    "bottom": ("waist", "ankles"),
    "dress": ("shoulders", "knees"),
    "full_body_outer": ("shoulders", "knees"),
}


def _classified(*products):
    """The classifier's answer: one row per photo, in upload order; an
    image with ``detail`` true is a close-up of a part of its product, one
    with ``look`` a full-body look photo (the reference model when it comes
    after the uploads). Each photo lists its own product among its items,
    unless it is a close-up or a look photo, and the (category, name,
    group) of its ``items``. Each garment gets the span of its category
    (``SPANS``)."""
    rows, garments = {}, []
    for group, (_pid, cat, imgs) in enumerate(products, start=1):
        if cat in SPANS:
            starts, ends = SPANS[cat]
            garments.append({"g": group, "starts": starts, "ends": ends})
        for img in imgs:
            alone = img.get("look") or img.get("detail")
            own = [] if alone else [(cat, f"{cat} item", group)]
            shown = [*own, *img.get("items", ())]
            rows[img["index"]] = {
                "c": "full_body_look" if img.get("look") else cat,
                "d": f"{cat} item",
                "s": img["view"],
                "w": img["presentation"] == "worn",
                "x": bool(img.get("detail")),
                "g": group,
                "items": [{"c": c, "d": d, "g": g} for c, d, g in shown],
            }
    return {"photos": [rows[i] for i in sorted(rows)], "garments": garments}


def _look(index, *items, view="front"):
    """A look photo (or the reference model) showing ``items``, each
    (category, name, group)."""
    return _img(index, view, "worn", look=True, items=items)


PLAN: dict[str, list] = {"items": []}


def _json_router(classification: dict):
    """Route generate_json calls by their system prompt."""

    def respond(parts, *, system=None, schema=None, params=None):
        if system and "product classifier" in system:
            return classification
        if system and "fashion stylist" in system:
            return PLAN
        raise AssertionError(f"unexpected generate_json call: {system!r}")

    return respond


def _images_counter():
    counter = iter(range(100))

    def respond(parts, *, system=None, params=None):
        return GENERATED[next(counter) % len(GENERATED)]

    return respond


def _run(req: TryOnRequest) -> list:
    async def collect():
        return [event async for event in pipeline.run_tryon(req)]

    return asyncio.run(collect())


def _request(images, person, n, **kw) -> TryOnRequest:
    kw.setdefault("policy", RunPolicy(num_variations=3))
    return TryOnRequest(product_images=images[:n], person_image=person, **kw)


def _types(events) -> list[str]:
    return [e.type for e in events]


def _image_calls(fake_gen) -> list[dict]:
    return [c for c in fake_gen.calls if c["fn"] == "generate_image"]


def _byte_parts(call) -> list[bytes]:
    return [p for p in call["raw_parts"] if isinstance(p, bytes)]


def test_single_route_events(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0), _img(1)]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    assert _types(events) == [
        "classified",
        "progress",
        "record",
        "variation",
        "variation",
        "variation",
        "complete",
    ]
    classified = events[0]
    assert isinstance(classified, ClassifiedEvent)
    assert classified.payload["mode"] == "single"
    assert classified.classification.products[0].refs[0].image == images[0]
    assert classified.payload["products"][0]["chosen"] == {
        "source": "upload",
        "presentation": "flat",
        "index": 0,
    }
    assert (
        isinstance(events[1], ProgressEvent)
        and events[1].stage == "preprocessed"
    )
    variations = [e for e in events if isinstance(e, VariationEvent)]
    assert all(
        v.status == "ready" and v.extra == {"view": "front"} for v in variations
    )
    complete = events[-1]
    assert isinstance(complete, CompleteEvent)
    assert complete.best_index == complete.ranked_indices[0]
    assert sorted(complete.ranked_indices) == [0, 1, 2]


def test_single_route_prompt_and_eval_inputs(fake_gen, fakes, images, person):
    fake_gen.json(
        _json_router(_classified(("A", "eyewear", [_img(0), _img(1)])))
    )
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2))
    call = _image_calls(fake_gen)[0]
    # body, face, primary, secondary: same side, so upload order
    assert _byte_parts(call) == [person, FACE_CROP, images[0], images[1]]
    assert call["system"].startswith("You are an expert fashion photographer")
    # Glasses too: the judge sees the face crop (and what they leave visible).
    assert fakes.faces == [FACE_CROP] * 3
    assert all(c["product_filter"] == "eyewear" for c in fakes.product_calls)
    assert fakes.product_calls[0]["references"] == [images[0]]
    # The body is the photo as it is; both faces are one crop of it.
    assert fakes.faces_cut == [person]


def test_no_face_found_sends_the_body_only(
    fake_gen, fakes, images, person, monkeypatch
):
    monkeypatch.setattr(media, "face_box", lambda data: None)
    fake_gen.json(_json_router(_classified(("A", "eyewear", [_img(0)]))))
    fake_gen.image(_images_counter())
    _run(_request(images[:1], person, 1))
    assert _byte_parts(_image_calls(fake_gen)[0]) == [person, images[0]]
    assert fakes.faces == [None] * 3  # no reference face, no face image


def test_product_photos_reach_the_models_cropped(
    fake_gen, fakes, images, person
):
    buf = io.BytesIO()
    framed = Image.new("RGB", (400, 400), (255, 255, 255))
    framed.paste((30, 30, 30), (150, 100, 250, 300))
    framed.save(buf, format="PNG")
    photo = buf.getvalue()
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    _run(_request([photo], person, 1))
    cropped = media.trim(photo)
    assert Image.open(io.BytesIO(cropped)).size == (116, 216)
    classify = next(c for c in fake_gen.calls if c["fn"] == "generate_json")
    assert _byte_parts(classify) == [cropped]
    assert _byte_parts(_image_calls(fake_gen)[0])[2] == cropped
    assert fakes.product_calls[0]["references"] == [cropped]


def test_best_index_is_top_ranked_ready(
    fake_gen, fakes, images, person, monkeypatch
):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    imperfections = {GENERATED[0]: 2, GENERATED[1]: 0, GENERATED[2]: 1}

    def evaluate_outfit(products, target, **_kw):
        return Verdict(publish=True, imperfection_count=imperfections[target])

    monkeypatch.setattr(outfit, "evaluate_outfit", evaluate_outfit)
    events = _run(_request(images, person, 1))
    media_by_index = {e.index: e.media for e in events if e.type == "variation"}
    complete = events[-1]
    assert media_by_index[complete.best_index] == GENERATED[1]
    scores = {e.media: e.score for e in events if e.type == "variation"}
    assert scores[GENERATED[1]] > scores[GENERATED[2]] > scores[GENERATED[0]]


def test_outfit_judge_runs_once_per_variation(
    fake_gen, fakes, images, person, monkeypatch
):
    # Real outfit judge: its judge-model call is the faked call.
    monkeypatch.setattr(outfit, "evaluate_outfit", REAL_OUTFIT_JUDGE)
    router = _json_router(
        _classified(
            ("A", "top", [_img(0), _img(1), _img(2, "back")]),
            ("B", "bottom", [_img(3), _img(4, presentation="worn")]),
        )
    )
    judged = []

    def respond(parts, *, system=None, schema=None, params=None):
        if schema is outfit.WEARER_SCHEMA:
            judged.append((parts, params))
            return {
                "products": [],
                "publish": True,
                "imperfection_count": 0,
                "description": "",
            }
        return router(parts, system=system, schema=schema, params=params)

    fake_gen.json(respond)
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 5))
    variations = [e for e in events if e.type == "variation"]
    assert [v.status for v in variations] == ["ready"] * 3
    # One judge call per variation, every product in it: the best front
    # upload of each (A the first, B worn before flat), labelled by category.
    assert len(judged) == 3
    sent = [media.to_png(image) for image in images]  # the judge's PNGs
    top, bottom = PRODUCT_FILTER_HINTS["top"], PRODUCT_FILTER_HINTS["bottom"]
    for parts, params in judged:
        assert parts[0].startswith(f"PRODUCT_1 ({top})")
        assert parts[2].startswith(f"PRODUCT_2 ({bottom})")
        assert [parts[1], parts[3]] == [sent[0], sent[4]]
        assert parts[4] == "TARGET (the generated image):"
        # Then the wearer as the drawing got it: the photo as it is, and
        # its face crop.
        assert parts[6].startswith("WEARER (the person before the try-on")
        assert parts[7] == media.to_png(person)
        assert parts[8] == outfit.FACE_LABEL
        assert parts[9] == media.to_png(FACE_CROP)
        assert parts[10] == outfit.WEARER_NOTE
        assert params.thinking == "MEDIUM"
    assert "hairstyle" not in variations[0].verdict.details


def test_eval_without_a_front_upload_uses_the_best_upload(
    fake_gen, fakes, images, person
):
    classification = _classified(
        ("A", "top", [_img(0, "back"), _img(1, "other")])
    )
    fake_gen.json(_json_router(classification))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    _run(
        _request(
            images,
            person,
            2,
            reference_model_image=model,
            generate_back=False,
        )
    )
    # No front upload: the first upload; never the reference crop.
    assert [c["references"] for c in fakes.product_calls] == [[images[0]]] * 3


def test_back_pass_when_back_refs_exist(fake_gen, fakes, images, person):
    fake_gen.json(
        _json_router(
            _classified(
                ("A", "top", [_img(0), _img(1, "back"), _img(2, "back")])
            )
        )
    )
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 3))
    assert _types(events) == [
        "classified",
        "progress",
        "record",
        *["variation"] * 3,
        "progress",
        *["variation"] * 3,
        "complete",
    ]
    assert events[6].stage == "back"
    assert [e.message for e in events if e.type == "progress"] == [
        "Generating the front view",
        "Generating the back view",
    ]
    front = events[3:6]
    back = events[7:10]
    assert all(e.extra["view"] == "front" for e in front)
    assert all(e.extra["view"] == "back" for e in back)
    assert "ranked" not in back[0].extra and "ranked" not in back[1].extra
    assert sorted(back[-1].extra["ranked"]) == [0, 1, 2]
    assert back[-1].extra["best_index"] in (0, 1, 2)
    complete = events[-1]
    best_front = {e.index: e.media for e in front}[complete.best_index]
    back_call = _image_calls(fake_gen)[-1]
    # Only the back photos and the best front: no wearer body or face.
    assert _byte_parts(back_call) == [images[1], images[2], best_front]
    assert "WEARER" not in back_call["system"] + _text_parts(back_call)
    back_refs = fakes.product_calls[-1]["references"]
    assert back_refs == [images[1]]  # the best back upload only
    # The front is judged against the wearer, the back without it.
    assert fakes.wearers == [person] * 3 + [None] * 3


def test_a_garment_is_framed_by_its_span_front_and_back(
    fake_gen, fakes, images, person
):
    """The dress (shoulders to knees) is framed head to mid-calf in both
    views: no plain shoes, no ground shadow."""
    fake_gen.json(
        _json_router(_classified(("A", "dress", [_img(0), _img(1, "back")])))
    )
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2))
    front, back = _image_calls(fake_gen)[0], _image_calls(fake_gen)[-1]
    framed = (
        "## FRAMING\nFrom the top of the head to mid-calf. Arms and hands "
        "visible, relaxed. The product is fully in frame.\n"
    )
    for call in (front, back):
        text = _text_parts(call)
        assert framed in text
        assert "plain shoes" not in text and "on the ground" not in text
        assert (
            "you MUST state the framing: where the top and the bottom edge "
            "of the photo fall, exactly as the FRAMING section says."
        ) in call["system"]


def test_back_pass_judge_gets_no_face(fake_gen, fakes, images, person):
    fake_gen.json(
        _json_router(_classified(("A", "top", [_img(0), _img(1, "back")])))
    )
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    back = [
        e for e in events if e.type == "variation" and e.extra["view"] == "back"
    ]
    assert len(back) == 3
    assert fakes.faces == [FACE_CROP] * 3 + [None] * 3  # the front only


def test_back_pass_needs_an_uploaded_back_photo(
    fake_gen, fakes, images, person
):
    classification = _classified(("A", "top", [_img(0)]))
    fake_gen.json(_json_router(classification))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    events = _run(_request(images, person, 1, reference_model_image=model))
    refs = events[0].payload["products"][0]["refs"]
    assert [r["view"] for r in refs] == ["front", "front"]  # upload, crop
    assert "back" not in [e.stage for e in events if e.type == "progress"]
    assert len(_image_calls(fake_gen)) == 3


def test_back_pass_sends_no_look_photo_crop(fake_gen, fakes, images, person):
    classification = _classified(("A", "top", [_img(0), _img(1, "back")]))
    fake_gen.json(_json_router(classification))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    _run(_request(images, person, 2, reference_model_image=model))
    front, back = _image_calls(fake_gen)[0], _image_calls(fake_gen)[-1]
    assert CROPS[0] in _byte_parts(front)  # the look's top band
    assert _byte_parts(back)[0] == images[1]  # then the best front only
    assert len(_byte_parts(back)) == 2
    assert "REFERENCE CROP" not in back["system"] + _text_parts(back)


def test_no_back_pass_without_back_refs_or_when_disabled(
    fake_gen, fakes, images, person
):
    fake_gen.json(
        _json_router(_classified(("A", "top", [_img(0), _img(1, "back")])))
    )
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2, generate_back=False))
    assert "back" not in [e.stage for e in events if e.type == "progress"]
    assert len(_image_calls(fake_gen)) == 3

    fake_gen.json(_json_router(_classified(("A", "top", [_img(0), _img(1)]))))
    events = _run(_request(images, person, 2))
    assert [e.extra["view"] for e in events if e.type == "variation"] == [
        "front"
    ] * 3


@pytest.mark.parametrize(
    ("products", "has_back"),
    [
        ([("A", "top", [_img(0), _img(1, "back")])], True),
        ([("A", "top", [_img(0), _img(1)])], False),
        ([("A", "top", [_img(0)]), ("B", "bottom", [_img(1, "back")])], False),
    ],
)
def test_a_back_view_can_be_drawn_from_any_front(
    fake_gen, fakes, images, person, products, has_back
):
    """``run_tryon`` sets ``prepared`` before its first front (no back pass
    of its own here); ``back_view`` then draws the back of a chosen front."""
    fake_gen.json(_json_router(_classified(*products)))
    fake_gen.image(_images_counter())
    prepared = Prepared()
    seen = []

    async def collect():
        req = _request(images, person, 2, generate_back=False)
        async for event in pipeline.run_tryon(req, prepared):
            if event.type == "variation":
                seen.append(prepared.ctx is not None)
        if not has_back:
            return []
        drawn = pipeline.back_view(prepared, GENERATED[2])
        return [event async for event in drawn]

    back = asyncio.run(collect())
    assert seen == [True] * 3
    assert prepared.has_back is has_back
    assert prepared.policy.num_variations == 3
    if not has_back:
        return
    assert [e.extra["view"] for e in back] == ["back"] * 3
    assert sorted(back[-1].extra["ranked"]) == [0, 1, 2]
    assert _byte_parts(_image_calls(fake_gen)[-1])[-1] == GENERATED[2]


def test_no_back_pass_without_a_ready_front(fake_gen, fakes, images, person):
    fakes.publish = False
    fake_gen.json(
        _json_router(_classified(("A", "top", [_img(0), _img(1, "back")])))
    )
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    assert [e.status for e in events if e.type == "variation"] == [
        "rejected"
    ] * 3
    assert events[-1].best_index is None


OUTFIT = _classified(("A", "top", [_img(0)]), ("B", "bottom", [_img(1)]))


def test_outfit_without_reference_model_never_crops(
    fake_gen, fakes, images, person
):
    fake_gen.json(_json_router(OUTFIT))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    assert fakes.crop_calls == []
    assert events[0].payload["mode"] == "outfit"
    assert events[0].payload["framing"] == "full_body"
    # full body, whatever its garments' spans
    assert FRAMING["full_body"] in _text_parts(_image_calls(fake_gen)[0])
    assert events[-1].type == "complete"
    assert not any(e.type == "progress" and e.stage == "back" for e in events)


def test_outfit_with_reference_model_matches_bands_by_category(
    fake_gen, fakes, images, person
):
    model = png(20, 40, color=(9, 9, 9))
    fake_gen.json(_json_router(OUTFIT))  # A top, B bottom
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2, reference_model_image=model))
    assert fakes.crop_calls == [model]
    assert fakes.asked == [([], True)]  # its halves only: it lists nothing
    # the classifier sees the reference model last, never the crops
    classify_call = fake_gen.calls[0]
    assert [p for p in classify_call["raw_parts"] if isinstance(p, bytes)] == [
        images[0],
        images[1],
        model,
    ]
    chosen = [p["chosen"] for p in events[0].payload["products"]]
    assert chosen == [  # the uploads; the crops go along as extra views
        {"source": "upload", "presentation": "flat", "index": 0},
        {"source": "upload", "presentation": "flat", "index": 1},
    ]
    call = _image_calls(fake_gen)[0]
    assert _byte_parts(call)[2:] == [  # A, B, then the look-photo crops
        _drawn(image) for image in (images[0], images[1], CROPS[0], CROPS[2])
    ]
    prompt = "\n".join(p for p in call["raw_parts"] if isinstance(p, str))
    assert (
        "- image_4.png — REFERENCE CROP: PRODUCT A top item as worn by another"
        in prompt
    )
    assert (
        "- image_5.png — REFERENCE CROP: PRODUCT B bottom item as worn by "
        "another" in prompt
    )
    assert (
        "- image_2.png — PRODUCT A (top item): worn exactly as in its "
        "REFERENCE CROP." in prompt
    )
    bottom_refs = [
        c["references"]
        for c in fakes.product_calls
        if images[1] in c["references"]
    ]
    assert bottom_refs and all(CROPS[2] not in refs for refs in bottom_refs)


def _drawn(image: bytes) -> bytes:
    """``image`` as an outfit's drawing gets it alone (``cards.single``)."""
    return cards.single(cards.tight(image))


def test_an_outfit_sends_one_image_per_product(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(OUTFIT))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2))
    call = _image_calls(fake_gen)[0]
    # The body keeps its canvas (``cards.whole``); the rest is cut tight.
    assert _byte_parts(call) == [
        cards.single(cards.whole(person)),
        *(_drawn(image) for image in (FACE_CROP, *images[:2])),
    ]
    assert (
        "you MUST explicitly reference all 4 input filenames"
        in (call["system"])
    )
    assert "- image_3.png — PRODUCT B (bottom item)" in _text_parts(call)


@pytest.mark.parametrize(("ratio", "shape"), [("3:4", True), ("9:16", False)])
def test_an_outfit_names_the_3_4_portrait_only_for_a_3_4_image(
    fake_gen, fakes, images, person, ratio, shape
):
    fake_gen.json(_json_router(OUTFIT))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2, image=ImageParams(aspect_ratio=ratio)))
    call = _image_calls(fake_gen)[0]
    assert call["params"].aspect_ratio == ratio
    text = _text_parts(call)
    assert ("Vertical 3:4 full-length portrait." in text) is shape


def test_no_face_in_person_sends_no_face_to_the_judge(
    fake_gen, fakes, images, person
):
    fakes.face_box = None
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 1))
    assert fakes.faces == [None] * 3
    assert all(e.status == "ready" for e in events if e.type == "variation")


@pytest.mark.parametrize(
    ("category", "framing"),
    [("jewelry_head", "head"), ("top", "upper_body"), ("dress", "full_body")],
)
def test_the_judge_is_the_only_person_check_when_the_head_shows(
    fake_gen, fakes, images, person, category, framing
):
    fake_gen.json(_json_router(_classified(("A", category, [_img(0)]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 1))
    assert events[0].payload["framing"] == framing
    # One text call (the classifier): no hairstyle or same-person judge.
    calls = [c["fn"] for c in fake_gen.calls if c["fn"] != "generate_image"]
    assert calls == ["generate_json"]
    assert fakes.faces == [FACE_CROP] * 3
    variations = [e for e in events if e.type == "variation"]
    assert [v.status for v in variations] == ["ready"] * 3
    assert "hairstyle" not in variations[0].verdict.details


def test_verdict_details_and_score(fake_gen, fakes, images, person):
    fakes.imperfections = 2
    fake_gen.json(_json_router(OUTFIT))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    v = next(e for e in events if e.type == "variation")
    assert v.verdict.imperfection_count == 2
    assert [p["product_id"] for p in v.verdict.details["products"]] == [
        "A",
        "B",
    ]
    assert v.score == quality_score(True, 2) == 70.0
    assert v.verdict.details["score"] == v.score


def test_score_without_face_and_rejected(fake_gen, fakes, images, person):
    fakes.face_box = None
    fakes.publish = False
    fakes.imperfections = 1
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 1))
    v = next(e for e in events if e.type == "variation")
    assert v.score == quality_score(False, 1) == 55.0
    assert "face_score" not in v.verdict.details


@pytest.mark.parametrize("n", [0, 21])
def test_input_error_on_product_image_count(fake_gen, fakes, images, person, n):
    events = _run(_request(images * 3, person, n))  # 27 photos to cut from
    assert _types(events) == ["error"]
    assert isinstance(events[0], ErrorEvent)
    assert "product image" in events[0].message
    assert fake_gen.calls == []


def test_input_error_on_empty_images(fake_gen, fakes, images, person):
    events = _run(
        TryOnRequest(product_images=[images[0], b""], person_image=person)
    )
    assert _types(events) == ["error"]
    events = _run(TryOnRequest(product_images=[images[0]], person_image=b""))
    assert _types(events) == ["error"]


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (GenerationError("model overloaded"), "model overloaded"),
        (RuntimeError("could not read gs://secret/creds.json"), SERVER_FAILED),
    ],
)
def test_an_error_event_shows_only_messages_meant_for_the_user(
    fake_gen, fakes, images, person, caplog, error, shown
):
    fake_gen.json(error)
    events = _run(_request(images, person, 1))
    assert _types(events) == ["error"]
    assert events[0].message == shown
    assert str(error) in caplog.text  # the details are logged


def test_image_params_reach_generate_image_unchanged(
    fake_gen, fakes, images, person
):
    params = ImageParams(aspect_ratio="1:1", image_size="1K", thinking="LOW")
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 1, image=params))
    assert fake_gen.calls[-1]["fn"] == "generate_image"
    assert fake_gen.calls[-1]["params"] is params
    assert all(c["params"] is params for c in _image_calls(fake_gen))


def test_default_image_params(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 1))
    assert _image_calls(fake_gen)[0]["params"] == ImageParams()


def test_generation_error_in_one_variation(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image([GENERATED[0], GenerationError("no image"), GENERATED[1]])
    events = _run(
        _request(images, person, 1, policy=RunPolicy(mode="sequential"))
    )
    # sequential: first ready stops the run
    assert [e.status for e in events if e.type == "variation"] == ["ready"]

    fake_gen.image([GenerationError("no image"), GENERATED[0], GENERATED[1]])
    events = _run(_request(images, person, 1))
    statuses = sorted(e.status for e in events if e.type == "variation")
    assert statuses == ["error", "ready", "ready"]
    assert events[-1].type == "complete"


def test_failure_outside_runner_yields_error_event(
    fake_gen, fakes, images, person
):
    fake_gen.json(GenerationError("classifier down", retryable=True))
    events = _run(_request(images, person, 1))
    assert _types(events) == ["error"]
    assert events[0].message == "classifier down"
    assert events[0].retryable is True


def test_scenario_and_guidelines_fall_back_to_settings(
    fake_gen, fakes, images, person, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_SCENARIO", "a sunny beach")
    monkeypatch.setenv("GENMEDIA_STYLE_GUIDELINES", "No logos.")
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 1))
    text = _image_calls(fake_gen)[0]["raw_parts"][-1]
    assert "a sunny beach" in text and "No logos." in text

    _run(
        _request(
            images, person, 1, scenario="a city street", style_guidelines=""
        )
    )
    text = _image_calls(fake_gen)[-1]["raw_parts"][-1]
    assert "a city street" in text and "Brand styling rules" not in text


@pytest.mark.parametrize(
    ("env", "request_guidelines", "guided"),
    [
        ("", None, False),
        ("Coats open.", None, True),
        ("", "Coats open.", True),
    ],
)
def test_guidelines_from_env_or_request_replace_the_outerwear_default(
    fake_gen,
    fakes,
    images,
    person,
    monkeypatch,
    env,
    request_guidelines,
    guided,
):
    monkeypatch.setenv("GENMEDIA_STYLE_GUIDELINES", env)
    fake_gen.json(
        _json_router(_classified(("A", "full_body_outer", [_img(0)])))
    )
    fake_gen.image(_images_counter())
    req = _request(images, person, 1, style_guidelines=request_guidelines)
    _run(req)
    text = _image_calls(fake_gen)[0]["raw_parts"][-1]
    default = "worn as its product photo shows it (closed stays closed)"
    assert (default in text) is not guided
    assert ("\n\nBrand styling rules: Coats open.\n\n" in text) is guided


def test_the_styling_plan_gets_the_brand_rules(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(OUTFIT))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2, style_guidelines="Coats open."))
    (plan_call,) = [
        c for c in fake_gen.calls if "fashion stylist" in (c["system"] or "")
    ]
    assert plan_call["raw_parts"][-2] == "BRAND STYLING RULES: Coats open."


def test_run_deadline_bounds_generation(fake_gen, fakes, images, person):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))

    def slow(parts, *, system=None, params=None):
        time.sleep(0.3)
        return GENERATED[0]

    fake_gen.image(slow)
    policy = RunPolicy(num_variations=2, deadline_s=0.2)
    events = _run(_request(images, person, 1, policy=policy))
    variations = [e for e in events if e.type == "variation"]
    assert [v.status for v in variations] == ["error", "error"]
    assert all("exceeded" in v.error.lower() for v in variations)
    assert events[-1].type == "complete" and events[-1].best_index is None


def test_closing_the_stream_early_stops_cleanly(
    fake_gen, fakes, images, person
):
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())

    async def first_three():
        stream = pipeline.run_tryon(_request(images, person, 1))
        events = [await anext(stream) for _ in range(4)]
        await stream.aclose()
        return events

    events = asyncio.run(first_three())
    assert _types(events) == ["classified", "progress", "record", "variation"]


TOP_AND_BOTTOM = _classified(
    ("A", "top", [_img(0)]), ("B", "bottom", [_img(1)])
)
SHOES_ITEM = ("footwear", "lace-up shoes", 3)  # a new group: the look's
# The reference model (photo 2) shows lace-up shoes nobody uploaded.
TOP_BOTTOM_LOOK = _classified(
    ("A", "top", [_img(0), _look(2, SHOES_ITEM)]), ("B", "bottom", [_img(1)])
)


@pytest.mark.parametrize(
    ("options", "upscaled", "sent"),
    [({}, [], SHOES), ({"upscale_crops": True}, [SHOES], SHOES_2K)],
    ids=["crop-as-is", "upscale-crops"],
)
def test_complete_look_adds_the_products_only_the_reference_shows(
    fake_gen, fakes, images, person, options, upscaled, sent
):
    fakes.boxes = {"lace-up shoes": SHOES}
    fake_gen.json(_json_router(TOP_BOTTOM_LOOK))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    events = _run(
        _request(
            images,
            person,
            2,
            reference_model_image=model,
            **options,
        )
    )
    # on by default: the look photo's box call asks for the new item
    assert fakes.crop_calls == [model]
    assert fakes.asked == [([("footwear", "lace-up shoes")], True)]
    classify_calls = [
        c for c in fake_gen.calls if "product classifier" in (c["system"] or "")
    ]
    assert len(classify_calls) == 1  # no second classification
    assert fakes.upscaled == upscaled  # only with upscale_crops
    shoes = events[0].payload["products"][2]
    assert (shoes["id"], shoes["category"], shoes["description"]) == (
        "C",
        "footwear",
        "lace-up shoes",
    )
    assert shoes["chosen"] == {
        "source": "model_crop",
        "presentation": "worn",
        "band": "item-0",
    }
    assert _drawn(sent) in _byte_parts(_image_calls(fake_gen)[0])
    # every product drawn is checked, the added shoes on their crop
    assert {c["product_filter"] for c in fakes.product_calls} == {
        "top",
        "bottom",
        "footwear",
    }
    assert all(e.status == "ready" for e in events if e.type == "variation")


def test_an_uploaded_product_in_the_look_is_cropped_never_added(
    fake_gen, fakes, images, person
):
    """The look shows B, the uploaded shoes (its group): no new product;
    B's crop beats its flat photo and is not upscaled (not added)."""
    look = _look(2, ("footwear", "lace-up shoes", 2))
    answer = _classified(
        ("A", "top", [_img(0), look]), ("B", "footwear", [_img(1)])
    )
    crop = png(600, 300, color=(90, 60, 30))  # large enough to be a hero
    fakes.boxes = {"footwear item": crop}
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    events = _run(
        _request(
            images,
            person,
            2,
            reference_model_image=model,
            upscale_crops=True,
        )
    )
    products = events[0].payload["products"]
    assert [p["category"] for p in products] == ["top", "footwear"]
    assert fakes.asked == [([("footwear", "footwear item")], True)]
    assert products[1]["chosen"]["source"] == "model_crop"
    assert fakes.upscaled == []
    assert _drawn(crop) in _byte_parts(_image_calls(fake_gen)[0])


def test_without_complete_look_the_look_adds_nothing(
    fake_gen, fakes, images, person
):
    fakes.boxes = {"lace-up shoes": SHOES}
    fake_gen.json(_json_router(TOP_BOTTOM_LOOK))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    events = _run(
        _request(
            images,
            person,
            2,
            reference_model_image=model,
            complete_look=False,
        )
    )
    assert fakes.asked == [([], True)]  # its halves only
    assert len(events[0].payload["products"]) == 2
    assert fakes.upscaled == []


POLO_LOOK = {  # the real box call's answer: a polo boxed like the top half
    "upper_body": [153, 241, 515, 906],
    "lower_body": [450, 0, 1000, 1000],
    "items": [{"index": 0, "box": [155, 243, 513, 904]}],
}


JEANS_LOOK = {  # jeans boxed like the bottom half (the hem on the shoes)
    "upper_body": [100, 0, 500, 1000],
    "lower_body": [452, 300, 1000, 640],
    "items": [{"index": 0, "box": [460, 310, 960, 630]}],
}


def _cut_look(
    fake_gen,
    monkeypatch,
    images,
    person,
    *products,
    added=("top", "polo shirt", 9),
    answer=POLO_LOOK,
):
    """The run's payload products and its first drawing, the reference
    model (shows ``added``, a product nobody uploaded) boxed by the real
    box call (``answer``)."""
    monkeypatch.setattr(reference, "box_items", cut.box_items)
    shown = _look(len(products), added)
    first, *others = products
    classify = _json_router(
        _classified((*first[:2], [*first[2], shown]), *others)
    )
    fake_gen.json(
        lambda parts, **kw: (
            classify(parts, **kw) if kw.get("system") else answer
        )
    )
    fake_gen.image(_images_counter())
    model = png(1000, 2000, color=(9, 9, 9))
    req = _request(images, person, len(products), reference_model_image=model)
    events = _run(req)
    return events[0].payload["products"], _image_calls(fake_gen)[0]


def test_an_added_top_boxed_like_the_top_half_is_drawn_once_as_itself(
    fake_gen, fakes, images, person, monkeypatch
):
    """The polo's photo is the top half, labelled as the polo, and no top
    REFERENCE CROP is sent."""
    products, call = _cut_look(
        fake_gen, monkeypatch, images, person, ("A", "bottom", [_img(0)])
    )
    assert [p["chosen"] for p in products] == [
        {"source": "upload", "presentation": "flat", "index": 0},
        {"source": "model_crop", "presentation": "worn", "band": "top"},
    ]
    assert len(_byte_parts(call)) == 5  # body, face, A, B, A's bottom half
    text = _text_parts(call)
    assert (
        "- image_3.png — PRODUCT B (polo shirt), shown on a different person:"
        in text
    )
    assert text.count("REFERENCE CROP:") == 1
    assert "- image_4.png — REFERENCE CROP: PRODUCT A bottom item" in text


def test_an_added_bottom_boxed_like_the_bottom_half_is_drawn_once_as_itself(
    fake_gen, fakes, images, person, monkeypatch
):
    """The jeans' photo is the bottom half, labelled as the jeans, and no
    bottom REFERENCE CROP is sent."""
    products, call = _cut_look(
        fake_gen,
        monkeypatch,
        images,
        person,
        ("A", "top", [_img(0)]),
        added=("bottom", "jeans", 9),
        answer=JEANS_LOOK,
    )
    assert [p["chosen"] for p in products] == [
        {"source": "upload", "presentation": "flat", "index": 0},
        {"source": "model_crop", "presentation": "worn", "band": "bottom"},
    ]
    assert len(_byte_parts(call)) == 5  # body, face, A, B, A's top half
    text = _text_parts(call)
    assert (
        "- image_3.png — PRODUCT B (jeans), shown on a different person:"
        in text
    )
    assert text.count("REFERENCE CROP:") == 1
    assert "- image_4.png — REFERENCE CROP: PRODUCT A top item" in text


def test_an_uploaded_product_in_the_top_half_no_longer_keeps_it(
    fake_gen, fakes, images, person, monkeypatch
):
    """An uploaded necklace shows in the top half: the polo still takes the
    half as its photo, and the half is no REFERENCE CROP."""
    products, call = _cut_look(
        fake_gen,
        monkeypatch,
        images,
        person,
        ("A", "bottom", [_img(0)]),
        ("B", "jewelry_neck", [_img(1)]),
    )
    assert products[2]["chosen"] == {
        "source": "model_crop",
        "presentation": "worn",
        "band": "top",
    }
    assert len(_byte_parts(call)) == 6  # body, face, A, B, C, A's bottom half
    text = _text_parts(call)
    assert text.count("REFERENCE CROP:") == 1
    assert "- image_5.png — REFERENCE CROP: PRODUCT A bottom item" in text


STAMP = details.Detail("text", "MO", "front", "stamp", png(9, 9, (1, 1, 1)))
LOCK = details.Detail("hardware", "", "front", "padlock", png(9, 9, (2, 2, 2)))
WORN_LOCK = details.Detail(
    "hardware", "", "front", "worn", png(9, 9, (3, 3, 3))
)


def _text_parts(call) -> str:
    return "\n".join(p for p in call["raw_parts"] if isinstance(p, str))


def test_found_details_go_into_the_front_drawing_after_the_products(
    fake_gen, fakes, images, person
):
    images = [png(900, 1200, (20, 40, 60)), images[1]]  # crops far apart
    stamp = replace(STAMP, box=(50, 50, 100, 100))
    fakes.found = {images[0]: [stamp, replace(LOCK, box=(900, 900, 950, 950))]}
    answer = _classified(("A", "top", [_img(0), _img(1, "back")]))
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    assert fakes.searched == [[images[0]]]  # never its back photo
    front, back = _image_calls(fake_gen)[0], _image_calls(fake_gen)[-1]
    assert _byte_parts(front) == [
        person,
        FACE_CROP,
        images[0],
        STAMP.image,
        LOCK.image,
    ]
    text = _text_parts(front)
    assert (
        "- IMAGE 4 — DETAIL CLOSE-UP of PRODUCT A top item (text 'MO'; "
        "stamp): a crop of its product photo, shown enlarged; keep its "
        "shape, colours, materials and lettering at its true size on the "
        "product. Reproduce this detail exactly where it is visible on the "
        "product; never uncover it."
    ) in text
    assert (
        "- IMAGE 5 — DETAIL CLOSE-UP of PRODUCT A top item (hardware; padlock)"
        in text
    )
    # The back view gets none.
    assert _byte_parts(back)[0] == images[1]
    assert len(_byte_parts(back)) == 2  # and the best front, last
    assert "DETAIL CLOSE-UP" not in _text_parts(back)
    assert events[-1].type == "complete"


def test_the_front_drawing_waits_for_the_detail_search(
    fake_gen, fakes, images, person, monkeypatch
):
    finished = threading.Event()

    def slow_find(photos, *, limit=2, upscale=True, product="", anchor=None):
        time.sleep(0.3)  # still searching when the plan is done
        finished.set()
        return [LOCK]

    def draw(parts, *, system=None, params=None):
        assert finished.is_set(), "drew before the detail search ended"
        return GENERATED[0]

    monkeypatch.setattr(details, "find", slow_find)
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(draw)
    events = _run(_request(images, person, 1))
    call = _image_calls(fake_gen)[0]
    assert _byte_parts(call) == [person, FACE_CROP, images[0], LOCK.image]
    assert [e.status for e in events if e.type == "variation"] == ["ready"] * 3


def test_closeups_off_means_no_search(
    fake_gen, fakes, images, person, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_TRYON_CLOSEUPS", "false")
    fakes.found = {images[0]: [LOCK]}
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0)]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 1))
    assert fakes.searched == []
    call = _image_calls(fake_gen)[0]
    assert _byte_parts(call) == [person, FACE_CROP, images[0]]
    (kept,) = [e for e in events if isinstance(e, RecordEvent)]
    assert kept.payload["products"][0]["closeups"] is None  # not searched


def test_closeups_come_from_every_photo_of_each_product(
    fake_gen, fakes, person
):
    """One search per product over all its photos, its hero first; an
    outfit's close-ups go on their product's card (photos large enough for
    a 200 px crop to be a close-up)."""
    photos = [png(900, 1200, (20 + 25 * i, 40, 60)) for i in range(3)]
    crop = [png(200, 200, (i, i, i)) for i in range(3)]
    fakes.found = {
        photos[0]: [replace(LOCK, image=crop[0])],  # A, flat
        photos[1]: [replace(WORN_LOCK, image=crop[1])],  # A, worn: chosen
        photos[2]: [replace(STAMP, image=crop[2])],  # B
    }
    answer = _classified(
        ("A", "bag", [_img(0), _img(1, presentation="worn")]),
        ("B", "bottom", [_img(2)]),
    )
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(photos, person, 3))
    chosen = [p["chosen"]["index"] for p in events[0].payload["products"]]
    assert chosen == [1, 2]
    searched = dict(zip(fakes.told, fakes.searched, strict=True))
    assert (
        searched
        == {
            "bag item": [photos[1], photos[0]],  # the worn hero first
            "bottom item": photos[2:],
        }
    )
    call = _image_calls(fake_gen)[0]
    assert len(_byte_parts(call)) == 4  # body, face, one card per product
    text = _text_parts(call)
    assert (
        "- image_2.png — PRODUCT A (bag item) and its labelled detail "
        "close-ups beside it"
    ) in text
    # the flat photo's padlock too (the judge model saw it on the hero)
    assert "Details shown in image_2.png: A1 worn; A2 padlock." in text
    assert "Detail shown in image_3.png: B1 MO lettering (stamp)." in text


def test_the_run_record_keeps_the_photos_and_the_close_ups_used(
    fake_gen, fakes, images, person
):
    fakes.found = {images[1]: [WORN_LOCK], images[2]: [STAMP]}
    answer = _classified(
        ("A", "bag", [_img(0), _img(1, presentation="worn")]),
        ("B", "bottom", [_img(2)]),
    )
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 3))
    (kept,) = [e for e in events if isinstance(e, RecordEvent)]
    assert _types(events).index("record") < _types(events).index("variation")
    assert kept.payload["classification"] == events[0].payload
    source = record.read(kept.payload, kept.files)
    # The chosen photos (the uploads 1 and 2), as the try-on cropped them.
    assert source.photos == [kept.files["photo-1"], kept.files["photo-2"]]
    assert "photo-0" in kept.files  # every photo of each product
    # A's searched in its two photos, its worn hero first: found there
    assert source.closeups == [[replace(WORN_LOCK, photo=0)], [STAMP]]
    assert source.classification.framing == "full_body"


def _zoom_photo_run(fake_gen, fakes, person, found_in_zoom):
    """A (a skirt with a zoom photo) and B (a top, searched); the zoom
    photo's search finds ``found_in_zoom``."""
    photos = [png(900, 1200, (20 + 25 * i, 40, 60)) for i in range(3)]
    stamp = replace(STAMP, image=png(200, 200, (5, 5, 5)))
    fakes.found = {photos[2]: [stamp], photos[1]: found_in_zoom}
    answer = _classified(
        (
            "A",
            "bottom",
            [
                _img(0, presentation="worn"),
                _img(1, "other", detail=True),
            ],
        ),
        ("B", "top", [_img(2)]),
    )
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(photos, person, 3))
    searched = dict(zip(fakes.told, fakes.searched, strict=True))
    assert searched == {"bottom item": photos[:2], "top item": photos[2:]}
    refs = events[0].payload["products"][0]["refs"]
    assert len(refs) == 1  # the zoom photo is not one of A's photos
    call = _image_calls(fake_gen)[0]
    assert len(_byte_parts(call)) == 4  # body, face, one card per product
    return events, _text_parts(call)


def test_the_crop_found_in_a_zoom_photo_stays_on_its_card_whatever_its_size(
    fake_gen, fakes, person
):
    big = png(1800, 1800, (9, 9, 9))  # larger than A's photo
    where = "centre of the belt buckle"
    plaque = replace(STAMP, image=big, name="plaque", where=where)
    events, text = _zoom_photo_run(fake_gen, fakes, person, [plaque])
    assert f"Detail shown in image_2.png: A1 MO plaque ({where})." in text
    assert "Detail shown in image_3.png: B1 MO lettering (stamp)." in text
    (kept,) = [e for e in events if isinstance(e, RecordEvent)]
    (own,) = record.read(kept.payload, kept.files).closeups[0]
    assert (own.kind, own.name, own.where) == ("photo", "plaque", where)


def test_a_zoom_photo_with_nothing_found_in_it_is_dropped(
    fake_gen, fakes, person
):
    _, text = _zoom_photo_run(fake_gen, fakes, person, [])
    assert "A1" not in text
    assert "Detail shown in image_3.png: B1 MO lettering (stamp)." in text


def test_a_single_products_zoom_photo_gives_the_crop_found_in_it(
    fake_gen, fakes, images, person
):
    answer = _classified(
        ("A", "bottom", [_img(0), _img(1, "other", detail=True)])
    )
    plaque = replace(
        STAMP,
        image=png(30, 20, (7, 7, 7)),
        where="buckle on the belt",
        name="buckle",
    )
    fakes.found = {images[1]: [plaque]}
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    _run(_request(images, person, 2))
    assert fakes.searched == [images[:2]]  # its photo and its zoom photo
    assert fakes.told == ["bottom item"]
    call = _image_calls(fake_gen)[0]
    assert _byte_parts(call) == [
        person,
        FACE_CROP,
        images[0],
        plaque.image,
    ]
    assert (
        "- IMAGE 4 — DETAIL CLOSE-UP of PRODUCT A bottom item (MO buckle on "
        "the belt): a close-up photo of the product; keep its shape, "
        "colours, materials and lettering at its true size on the product."
    ) in _text_parts(call)


def test_the_look_photo_upload_is_boxed_while_the_close_ups_are_searched(
    fake_gen, fakes, images, person, monkeypatch
):
    """The box call and the search run at once; the products the look adds
    are never searched and no classification runs again."""
    searching = threading.Event()
    find, box = details.find, reference.box_items

    def slow_find(images_, **kwargs):
        searching.set()
        return find(images_, **kwargs)

    def box_while_searching(image, items=(), *, bands=False):
        assert searching.wait(timeout=5), "the box call waited for the search"
        return box(image, items, bands=bands)

    monkeypatch.setattr(details, "find", slow_find)
    monkeypatch.setattr(reference, "box_items", box_while_searching)
    fakes.boxes = {"lace-up shoes": SHOES}
    look = _look(1, ("bottom", "bottom item", 1), SHOES_ITEM)
    answer = _classified(("A", "bottom", [_img(0, presentation="worn"), look]))
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2))
    assert fakes.crop_calls == [images[1]]  # the look photo, as uploaded
    # A, worn from the front, needs no crop: the shoes only
    assert fakes.asked == [([("footwear", "lace-up shoes")], True)]
    assert fakes.searched == [[images[0]]]  # A's upload, not the shoes
    products = events[0].payload["products"]
    assert [(p["category"], p["chosen"]["source"]) for p in products] == [
        ("bottom", "upload"),
        ("footwear", "model_crop"),
    ]
    classify_calls = [
        c for c in fake_gen.calls if "product classifier" in (c["system"] or "")
    ]
    assert len(classify_calls) == 1


def test_without_complete_look_the_look_photo_upload_still_gives_its_halves(
    fake_gen, fakes, images, person
):
    answer = _classified(
        ("A", "bottom", [_img(0), _look(1, ("footwear", "loafers", 2))])
    )
    fakes.boxes = {"loafers": SHOES}
    fake_gen.json(_json_router(answer))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2, complete_look=False))
    assert (fakes.crop_calls, fakes.asked) == ([images[1]], [([], True)])
    assert events[0].payload["mode"] == "single"  # the loafers: not added
    refs = events[0].classification.products[0].refs
    assert [r.image for r in refs] == [images[0], CROPS[2]]


@pytest.mark.parametrize(
    ("products", "chosen"),
    [
        ([("A", "top", [_img(0), _img(2, "front", "worn", look=True)])], [0]),
        (
            [
                ("A", "top", [_img(0), _img(2, "front", "worn", look=True)]),
                ("B", "bottom", [_img(1)]),
            ],
            [0, 1],
        ),
    ],
    ids=["single", "outfit"],
)
def test_a_look_photo_is_never_a_product_photo(
    fake_gen, fakes, images, person, products, chosen
):
    """Worn and in A's group, it would win over A's flat upload: it is
    never chosen, drawn as a product, searched for close-ups or judged
    against."""
    fake_gen.json(_json_router(_classified(*products)))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 3, complete_look=False))
    payload = events[0].payload["products"]
    assert [p["chosen"]["index"] for p in payload] == chosen
    drawn = _byte_parts(_image_calls(fake_gen)[0])[2 : 2 + len(chosen)]
    if len(chosen) == 1:
        assert drawn == [images[0]]
    else:
        assert drawn == [_drawn(images[0]), _drawn(images[1])]
    look = events[0].classification.look_photo
    assert len(fakes.searched) == len(chosen)  # one search per product
    assert all(images[look] not in photos for photos in fakes.searched)
    judged = {r for c in fakes.product_calls for r in c["references"]}
    assert judged == {images[i] for i in chosen}


def test_a_lone_look_photo_is_the_outfit_to_try_on(
    fake_gen, fakes, images, person
):
    """Without complete_look too, every product it shows is cut from it."""
    fakes.boxes = {"midi skirt": CROPS[1], "lace-up shoes": SHOES}
    look = _look(
        0, ("bottom", "midi skirt", 2), ("footwear", "lace-up shoes", 3)
    )
    fake_gen.json(_json_router(_classified(("A", "dress", [look]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 1, complete_look=False))
    assert fakes.crop_calls == [images[0]]
    assert fakes.asked == [
        ([("bottom", "midi skirt"), ("footwear", "lace-up shoes")], True)
    ]
    products = events[0].payload["products"]
    assert [(p["category"], p["chosen"]["source"]) for p in products] == [
        ("bottom", "model_crop"),
        ("footwear", "model_crop"),
    ]
    # every product is judged against its crop (3 variations)
    judged = [
        (c["references"], c["product_filter"]) for c in fakes.product_calls
    ]
    assert judged == [([CROPS[1]], "bottom"), ([SHOES], "footwear")] * 3
    assert events[-1].type == "complete"


def test_a_lone_look_photo_with_no_product_is_an_error(
    fake_gen, fakes, images, person
):
    answer = _classified(("A", "dress", [_img(0, "front", "worn", look=True)]))
    fake_gen.json(_json_router(answer))
    events = _run(_request(images, person, 1))  # it lists no item
    assert _types(events) == ["error"]
    assert events[0].message == "no product to try on was found in the photos"
    assert _image_calls(fake_gen) == []


def test_one_product_with_a_model_photo_and_no_complete_look_gets_its_half(
    fake_gen, fakes, images, person
):
    """The one-product flow, framed for its type, with only the half of the
    model photo that shows it."""
    look = _look(1, SHOES_ITEM)  # the reference model: never added
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0), look]))))
    fake_gen.image(_images_counter())
    model = png(20, 40, color=(9, 9, 9))
    events = _run(
        _request(
            images, person, 1, reference_model_image=model, complete_look=False
        )
    )
    assert fakes.asked == [([], True)]  # the halves only
    assert (events[0].payload["mode"], events[0].payload["framing"]) == (
        "single",
        "upper_body",
    )
    call = _image_calls(fake_gen)[0]
    assert _byte_parts(call) == [person, FACE_CROP, images[0], CROPS[0]]
    assert "REFERENCE CROP: PRODUCT A top item as worn" in _text_parts(call)


def test_a_crop_hero_is_drawn_and_judged_with_its_uploads_close_ups(
    fake_gen, fakes, images, person
):
    """The look photo shows the top whole: its crop beats its flat upload
    as the hero (the primary image, the judge's), the upload goes second;
    the search gets the look photo first, then the upload, and the
    close-ups found in the upload stay."""
    crop = png(500, 600, color=(5, 50, 5))
    fakes.boxes = {"top item": crop}
    fakes.found = {images[0]: [LOCK]}
    look = _look(1, ("top", "top item", 1))
    fake_gen.json(_json_router(_classified(("A", "top", [_img(0), look]))))
    fake_gen.image(_images_counter())
    events = _run(_request(images, person, 2, complete_look=False))
    assert fakes.asked == [([("top", "top item")], True)]
    assert fakes.searched == [[images[1], images[0]]]  # never the crop
    assert events[0].payload["products"][0]["chosen"] == {
        "source": "model_crop",
        "presentation": "worn",
        "band": "item-0",
    }
    sent = _byte_parts(_image_calls(fake_gen)[0])
    assert sent[2:] == [crop, images[0], CROPS[0], LOCK.image]  # top half
    assert {r for c in fakes.product_calls for r in c["references"]} == {crop}
