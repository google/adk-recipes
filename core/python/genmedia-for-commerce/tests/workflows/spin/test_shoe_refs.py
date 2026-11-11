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
import json
import threading

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import media
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.models import get_models
from genmedia4commerce.workflows.spin import shoe_refs
from genmedia4commerce.workflows.spin import shoe_split as split
from genmedia4commerce.workflows.spin.shoe_prompts import (
    CLASSIFY_SCHEMA,
    CLASSIFY_SYSTEM,
    FOOT_SCHEMA,
    FOOT_SYSTEM,
    MASK_ASK,
    MASK_SCHEMA,
    NANO_ERASE_PROMPT,
    NOT_STANDING,
    PAIRWISE_SCHEMA,
    PAIRWISE_SYSTEM,
    STANDING_VIEWS,
    classify_prompt,
    twin_prompt,
)
from tests.conftest import trace_file, tracing

W, H = 500, 250  # every photo; the masks are drawn in pixels of this size
WHITE = (255, 255, 255)
RED, BLUE, GREEN = (200, 30, 30), (30, 30, 200), (30, 160, 30)
COLOURS = [(40 * k, 100, 255 - 40 * k) for k in range(6)]


def _encode(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _photo(*shoes) -> bytes:
    """A white photo with each (x0, y0, x1, y1, colour) shoe."""
    arr = np.full((H, W, 3), 255, np.uint8)
    for x0, y0, x1, y1, colour in shoes:
        arr[y0:y1, x0:x1] = colour
    return _encode(arr)


def _single(colour) -> bytes:
    return _photo((150, 60, 350, 190, colour))


def _lopsided(colour) -> bytes:
    """A single shoe with its collar on the left: its mirror differs."""
    return _photo((150, 110, 350, 190, colour), (150, 60, 230, 110, colour))


def _mirror_of_input(parts, **kw) -> bytes:
    """The image model's twin: the input mirrored."""
    return _encode(np.asarray(split.open_rgb(parts[1]))[:, ::-1].copy())


def _colour(image: bytes) -> tuple[int, ...]:
    """The most frequent colour that is not near white (else WHITE)."""
    arr = np.asarray(Image.open(io.BytesIO(image)).convert("RGB"))
    colours, counts = np.unique(arr.reshape(-1, 3), axis=0, return_counts=True)
    keep = (colours < 250).any(axis=1)
    if not keep.any():
        return WHITE
    return tuple(int(v) for v in colours[keep][np.argmax(counts[keep])])


def _mask(x0, y0, x1, y1, dx: int = 0) -> dict:
    """The mask answer of a shoe at (x0, y0, x1, y1); ``dx`` shifts its
    polygon (not its box) sideways."""
    nx, ny = (lambda x: round(x * 1000 / W)), (lambda y: round(y * 1000 / H))
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return {
        "box_2d": [ny(y0), nx(x0), ny(y1), nx(x1)],
        "mask": [[nx(x + dx), ny(y)] for x, y in corners],
        "label": "shoe",
        "complete": True,
    }


class _Judge:
    """The judge model's answers, keyed by what each call is shown.

    ``labels``: the photos' labels (first labelling call), by index;
    ``views``: a shoe's view by its colour (in a mask answer: the colour
    inside its box; later labelling calls: the piece's); ``masks``: per
    photo, the mask answers in turn (the last repeats); ``feet``: by colour
    (default right); ``pairs``: by the two colours (default same_side).
    """

    def __init__(self):
        self.labels: list[str] = []
        self.views: dict = {}
        self.masks: dict[bytes, list] = {}
        self.feet: dict = {}
        self.pairs: dict = {}
        self.pieces: list[list] = []  # the colours of each piece call
        self.mask_calls: list[bytes] = []
        self._lock = threading.Lock()

    def __call__(self, parts, *, system=None, schema=None, params=None):
        if system == CLASSIFY_SYSTEM:
            return {"labels": self._label(parts[1::2])}
        if parts[0] == MASK_ASK:
            with self._lock:
                self.mask_calls.append(parts[1])
                queue = self.masks[parts[1]]
                answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, BaseException):
                raise answer
            return {"boxes": [self._viewed(item, parts[1]) for item in answer]}
        if system == FOOT_SYSTEM:
            foot = self.feet.get(_colour(parts[0]), "right")
            return {"explanation": "", "toe": "right", "side": "outer"} | {
                "foot": foot
            }
        if system == PAIRWISE_SYSTEM:
            pair = frozenset({_colour(parts[1]), _colour(parts[3])})
            answer = self.pairs.get(pair, "same_side")
            return {"explanation": "", "answer": answer}
        raise AssertionError(f"unexpected call: {parts[:1]}")

    def _viewed(self, item: dict, photo: bytes) -> dict:
        """A mask answer with the view of the colour inside its box."""
        y0, x0, y1, x1 = item["box_2d"]
        arr = np.asarray(Image.open(io.BytesIO(photo)).convert("RGB"))
        crop = arr[
            y0 * H // 1000 : y1 * H // 1000, x0 * W // 1000 : x1 * W // 1000
        ]
        view = self.views.get(_colour(_encode(crop.copy())), "invalid")
        return item | {"view": view}

    def _label(self, images):
        if not self.pieces and self.labels is not None:
            labels, self.labels = self.labels, None
        else:
            colours = [_colour(image) for image in images]
            self.pieces.append(colours)
            labels = [self.views.get(c, "invalid") for c in colours]
        return [{"image": i, "label": lab} for i, lab in enumerate(labels)]


@pytest.fixture
def judge(fake_gen) -> _Judge:
    answers = _Judge()
    fake_gen.json(answers)
    return answers


def _calls(fake_gen, system):
    return [c for c in fake_gen.calls if c.get("system") == system]


# ---------------------------------------------------------------- labels


def test_photos_are_labelled_in_one_call_and_singles_trimmed(fake_gen, judge):
    photos = [_single(RED), _single(BLUE), _single(GREEN)]
    judge.labels = ["right", "invalid", "front"]
    refs = shoe_refs.build(photos)
    assert list(refs.images) == ["right", "front"]
    assert refs.mirrored == frozenset()
    pad = round(0.04 * 200)  # trimmed to the shoe (200 x 130) + 4 %
    right = Image.open(io.BytesIO(refs.images["right"]))
    assert right.size == (200 + 2 * pad, 130 + 2 * pad)
    assert _colour(refs.images["front"]) == GREEN
    [call] = _calls(fake_gen, CLASSIFY_SYSTEM)
    labelled = [f"IMAGE_{i}:" for i in range(3)]
    assert call["raw_parts"][0::2] == labelled
    assert call["raw_parts"][1::2] == photos
    assert call["schema"] == CLASSIFY_SCHEMA
    assert call["params"].model == get_models().judge
    assert call["params"].thinking == "MEDIUM"


def test_photos_are_labelled_at_768_px(fake_gen, judge):
    big = io.BytesIO()
    Image.new("RGB", (1600, 800), "white").save(big, format="PNG")
    judge.labels = ["invalid"]
    assert shoe_refs.build([big.getvalue()]).images == {}
    sent = _calls(fake_gen, CLASSIFY_SYSTEM)[0]["raw_parts"][1]
    assert Image.open(io.BytesIO(sent)).size == (768, 384)


def test_a_photo_the_answer_skips_is_missing(fake_gen, judge):
    fake_gen.json([{"labels": [{"image": 1, "label": "right"}]}, judge])
    refs = shoe_refs.build([_single(RED), _single(BLUE)])
    assert [_colour(r) for r in refs.images.values()] == [BLUE]


def test_labelling_failure_propagates(fake_gen):
    fake_gen.json(GenerationError("judge down", retryable=True))
    with pytest.raises(GenerationError):
        shoe_refs.build([_single(RED)])


# ---------------------------------------------------------------- separate

SEPARATE = _photo((40, 80, 200, 180, RED), (300, 80, 460, 180, BLUE))
GOOD = [_mask(40, 80, 200, 180), _mask(300, 80, 460, 180)]
# Masks on plain background cover no shoe pixel: they fail (and cut white).
GHOSTS = [
    _mask(210, 5, 290, 60),
    _mask(210, 190, 290, 245),
    _mask(5, 5, 30, 30),
]
ONE_BAD = [GOOD[0], GHOSTS[0]]


def test_separate_shoes_are_each_cut_from_good_masks(fake_gen, judge):
    judge.labels = ["multiple_separate"]
    judge.masks[SEPARATE] = [GOOD]
    judge.views = {RED: "right", BLUE: "left"}
    refs = shoe_refs.build([SEPARATE])
    assert judge.mask_calls == [SEPARATE]  # good masks: one call
    assert judge.pieces == []  # the views came with the masks
    assert list(refs.images) == ["right", "left"]
    assert _colour(refs.images["right"]) == RED
    [call] = [c for c in fake_gen.calls if c["raw_parts"][0] == MASK_ASK]
    assert call["schema"] == MASK_SCHEMA
    params = call["params"]
    assert (params.max_output_tokens, params.deadline_s) == (65535, 240)


def test_a_bad_mask_is_asked_again_once_and_the_better_kept(judge):
    judge.labels = ["multiple_separate"]
    judge.masks[SEPARATE] = [ONE_BAD, GOOD]
    judge.views = {RED: "right", BLUE: "left"}
    refs = shoe_refs.build([SEPARATE])
    assert judge.mask_calls == [SEPARATE, SEPARATE]
    assert list(refs.images) == ["right", "left"]  # the second, good one


def test_when_both_attempts_fail_the_one_failing_less_is_kept(judge):
    judge.labels = ["multiple_separate"]
    judge.masks[SEPARATE] = [ONE_BAD, GHOSTS]
    judge.views = {RED: "right"}
    refs = shoe_refs.build([SEPARATE])
    assert len(judge.mask_calls) == 2  # never a third call
    assert _colour(refs.images["right"]) == RED  # the first: 1 failing


def test_a_drifted_polygon_fails_its_fit(judge):
    drifted = [GOOD[0], _mask(300, 80, 460, 180, dx=-150)]
    judge.labels = ["multiple_separate"]
    judge.masks[SEPARATE] = [drifted, GOOD]
    shoe_refs.build([SEPARATE])
    assert len(judge.mask_calls) == 2


def test_a_failed_mask_call_is_asked_again(judge):
    judge.labels = ["multiple_separate"]
    judge.masks[SEPARATE] = [GenerationError("overloaded"), GOOD]
    judge.views = {RED: "right"}
    refs = shoe_refs.build([SEPARATE])
    assert len(judge.mask_calls) == 2
    assert _colour(refs.images["right"]) == RED


# ---------------------------------------------------------------- touching

TOUCHING = _photo((40, 60, 260, 200, RED), (260, 100, 420, 200, BLUE))
ERASED = _photo((60, 70, 240, 190, GREEN))  # the image model's output


def _touching(fake_gen, judge, nano):
    judge.labels = ["multiple_touching"]
    judge.masks[TOUCHING] = [
        [_mask(40, 60, 260, 200), _mask(260, 100, 420, 200)]
    ]
    fake_gen.image(nano)
    return shoe_refs.build([TOUCHING])


def test_touching_shoes_use_the_image_model_when_it_reads_a_view(
    fake_gen, judge
):
    judge.views = {GREEN: "front_right", RED: "right"}
    refs = _touching(fake_gen, judge, ERASED)
    assert judge.pieces == [[GREEN]]  # the fallback's view: from its mask
    assert list(refs.images) == ["front_right"]
    assert _colour(refs.images["front_right"]) == GREEN
    [call] = [c for c in fake_gen.calls if c["fn"] == "generate_image"]
    assert call["raw_parts"] == [NANO_ERASE_PROMPT, TOUCHING]
    params = call["params"]
    assert (params.aspect_ratio, params.image_size) == ("auto", "2K")
    assert len(judge.mask_calls) == 1  # no check, no second call


@pytest.mark.parametrize("label", ["multiple_touching", "invalid"])
def test_touching_shoes_fall_back_to_the_largest_cut(fake_gen, judge, label):
    judge.views = {GREEN: label, RED: "right"}
    refs = _touching(fake_gen, judge, ERASED)
    assert list(refs.images) == ["right"]
    assert _colour(refs.images["right"]) == RED  # the larger shoe


def test_a_refused_erase_leaves_the_fallback(fake_gen, judge):
    judge.views = {RED: "right"}
    refs = _touching(fake_gen, judge, GenerationError("blocked"))
    assert judge.pieces == []  # no image-model output to label
    assert _colour(refs.images["right"]) == RED


def test_nothing_usable_from_a_touching_photo(fake_gen, judge):
    refs = _touching(fake_gen, judge, ERASED)  # every piece invalid
    assert refs.images == {}


# ---------------------------------------------------------------- mirroring


def test_a_left_shoe_is_mirrored_only_on_a_conflict(fake_gen, judge):
    photos = [_single(RED), _lopsided(BLUE)]
    judge.labels = ["right", "right"]
    judge.feet = {BLUE: "left"}
    judge.pairs = {frozenset({RED, BLUE}): "opposite_sides"}
    fake_gen.image(_mirror_of_input)
    refs = shoe_refs.build(photos)
    assert list(refs.images) == ["right", "left"]
    assert refs.mirrored == frozenset({"left"})
    trimmed = split.png(media.crop_to_content(split.open_rgb(photos[1])))
    [twin] = [c for c in fake_gen.calls if c["fn"] == "generate_image"]
    assert twin["raw_parts"] == [twin_prompt("right", "left"), trimmed]
    mirrored = np.asarray(split.open_rgb(refs.images["left"]))
    assert (mirrored == np.asarray(split.open_rgb(trimmed))[:, ::-1]).all()
    [pair] = _calls(fake_gen, PAIRWISE_SYSTEM)
    assert [p for p in pair["raw_parts"] if isinstance(p, str)] == [
        "IMAGE_A:",
        "IMAGE_B:",
    ]
    assert pair["schema"] == PAIRWISE_SCHEMA
    assert _calls(fake_gen, FOOT_SYSTEM)[0]["schema"] == FOOT_SCHEMA


def test_a_side_whose_twin_fails_is_left_out(fake_gen, judge, debug_dir):
    judge.labels = ["right", "right"]
    judge.feet = {BLUE: "left"}
    judge.pairs = {frozenset({RED, BLUE}): "opposite_sides"}
    fake_gen.image(GenerationError("IMAGE_PROHIBITED_CONTENT"))
    with tracing() as root:
        refs = shoe_refs.build([_single(RED), _lopsided(BLUE)])
    assert list(refs.images) == ["right"] and not refs.mirrored
    notes = json.loads(trace_file(root, "_shoe-refs.json").read_text())
    assert notes["twin_failed"] == ["left"]


def test_without_a_conflict_a_left_shoe_is_used_as_is(judge):
    judge.labels = ["right", "front_right"]
    judge.feet = {BLUE: "left"}
    refs = shoe_refs.build([_single(RED), _single(BLUE)])
    assert list(refs.images) == ["right", "front_right"]
    assert refs.mirrored == frozenset()
    assert _colour(refs.images["front_right"]) == BLUE


def test_failed_foot_and_pair_calls_mean_unknown_and_unsure(fake_gen, judge):
    judge.labels = ["right", "right"]

    def answer(parts, **kw):
        if kw.get("system") in (FOOT_SYSTEM, PAIRWISE_SYSTEM):
            raise GenerationError("down")
        return judge(parts, **kw)

    fake_gen.json(answer)
    refs = shoe_refs.build([_single(RED), _single(BLUE)])
    assert list(refs.images) == ["right"] and not refs.mirrored


def test_pairs_only_within_a_family_and_the_first_five(fake_gen, judge):
    photos = [_single(c) for c in COLOURS] + [_single(RED), _single(BLUE)]
    judge.labels = ["right"] * 6 + ["left", "front"]
    shoe_refs.build(photos)
    assert len(_calls(fake_gen, PAIRWISE_SYSTEM)) == 10  # C(5, 2), R only
    assert len(_calls(fake_gen, FOOT_SYSTEM)) == 8


# ---------------------------------------------------------------- selection


def test_one_reference_per_view_in_generation_order(judge):
    labels = ["top_front", "back", "left", "right", "front", "front_right"]
    judge.labels = labels
    refs = shoe_refs.build([_single(c) for c in COLOURS])
    assert list(refs.images) == [
        "right",
        "front_right",
        "left",
        "front",
        "back",
    ]  # top_front skipped: there is a front


def test_top_front_without_a_front(judge):
    judge.labels = ["top_front", "back"]
    refs = shoe_refs.build([_single(RED), _single(BLUE)])
    assert list(refs.images) == ["back", "top_front"]


def test_a_photo_wins_over_a_cut_and_a_cut_over_an_erase(fake_gen, judge):
    judge.labels = ["multiple_touching", "multiple_separate", "front"]
    judge.masks[TOUCHING] = [[_mask(40, 60, 260, 200)]]
    judge.masks[SEPARATE] = [GOOD]
    judge.views = {GREEN: "right", RED: "right", BLUE: "front"}
    fake_gen.image(ERASED)
    refs = shoe_refs.build([TOUCHING, SEPARATE, _single(COLOURS[0])])
    # right: the separate cut (RED) over the erase (GREEN);
    # front: the photo over the cut (BLUE).
    assert _colour(refs.images["right"]) == RED
    assert _colour(refs.images["front"]) == COLOURS[0]


def _cand(view, source="photo", name="c"):
    return shoe_refs._Candidate(b"", view, source, name)


def test_as_is_comes_before_mirrored_whatever_the_source():
    cands = [_cand("right", name="a"), _cand("left", "nano", name="b")]
    cands += [_cand("right", name="c")]
    answers = {(0, 2): "opposite_sides"}
    chosen = shoe_refs._select(cands, ["left", "right", "right"], answers)
    assert [(v, m, c.name) for v, m, c in chosen] == [
        ("right", False, "c"),
        ("left", False, "b"),  # as is, over the mirrored photo "a"
    ]


def test_a_mirrored_shoe_fills_a_view_no_real_one_shows():
    # The boots case: the real right shoe shows the outer side three-quarter
    # (front_right) and the inner side (left); the mirrored left shoe fills
    # the profile (right) that no real photo shows.
    cands = [_cand("left", name="buckles"), _cand("left", name="inner")]
    cands += [_cand("front_right", "nano", name="real")]
    answers = {(0, 1): "opposite_sides"}
    chosen = shoe_refs._select(cands, ["left", "right", "right"], answers)
    assert [(v, m, c.name) for v, m, c in chosen] == [
        ("right", True, "buckles"),
        ("front_right", False, "real"),
        ("left", False, "inner"),
    ]


def test_a_mirrored_shoe_fills_a_side_no_real_one_shows_either():
    cands = [_cand("left", name="buckles"), _cand("left", name="inner")]
    answers = {(0, 1): "opposite_sides"}
    chosen = shoe_refs._select(cands, ["left", "right"], answers)
    assert [(v, m, c.name) for v, m, c in chosen] == [
        ("right", True, "buckles"),
        ("left", False, "inner"),
    ]


def test_the_known_flaw_every_conflicting_left_shoe_is_mirrored():
    cands = [_cand("right", name="a"), _cand("front_right", name="b")]
    chosen = shoe_refs._select(
        cands, ["left", "left"], {(0, 1): "opposite_sides"}
    )
    assert [(v, m) for v, m, _ in chosen] == [
        ("left", True),
        ("front_left", True),
    ]


def test_at_most_max_references(monkeypatch, judge):
    monkeypatch.setattr(shoe_refs, "MAX_REFERENCES", 2)
    judge.labels = ["back", "front", "right"]
    refs = shoe_refs.build([_single(RED), _single(BLUE), _single(GREEN)])
    assert list(refs.images) == ["right", "front"]


def test_the_decisions_are_traced(fake_gen, judge, debug_dir):
    judge.labels = ["right", "right"]
    judge.feet = {BLUE: "left"}
    judge.pairs = {frozenset({RED, BLUE}): "opposite_sides"}
    fake_gen.image(_mirror_of_input)
    with tracing() as root:
        shoe_refs.build([_single(RED), _lopsided(BLUE)])
    notes = json.loads(trace_file(root, "_shoe-refs.json").read_text())
    assert notes["labels"] == ["right", "right"]
    assert notes["feet"] == {"photo 0": "right", "photo 1": "left"}
    assert notes["pairs"] == {"photo 0 | photo 1": "opposite_sides"}
    assert notes["mirrored"] == ["left"]


def test_eval_mode_labels_only_the_standing_views(fake_gen):
    fake_gen.json({"labels": [{"image": 0, "label": "back_left"}]})
    assert shoe_refs.label_images([_single(RED)], eval_mode=True) == [
        "back_left"
    ]
    [call] = fake_gen.calls
    system, schema = classify_prompt(eval_mode=True)
    assert (call["system"], call["schema"]) == (system, schema)
    enum = schema["properties"]["labels"]["items"]["properties"]["label"]
    assert enum["enum"] == [*STANDING_VIEWS, NOT_STANDING]
    for word in ("invalid", "multiple", "top_front"):
        assert word not in system
    assert NOT_STANDING in system
    assert classify_prompt() == (CLASSIFY_SYSTEM, CLASSIFY_SCHEMA)
