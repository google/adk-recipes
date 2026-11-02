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

"""Worn product photos on a mannequin in the photo try-on (``worn``): the
image model's edit and the paste-back faked, the rest as in
``test_pipeline`` and ``test_cache``."""

import asyncio
import hashlib

import pytest

from genmedia4commerce.core import degraded
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.gen import ImageParams
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import (
    Classification,
    Photo,
    Product,
    ProductRef,
)
from genmedia4commerce.workflows.tryon import mannequin, skin, styling, worn
from genmedia4commerce.workflows.tryon.types import TryOnRequest
from tests.workflows.tryon import test_cache as tc
from tests.workflows.tryon import test_pipeline as tp

fakes = tp.fakes  # the faked media, eval and look-photo calls
store = tc.store  # the cache on, over the fake bucket
EDIT = png(32, 40, color=(170, 170, 170))  # the image model's edit
SINGLE = tp._classified(
    ("A", "top", [tp._img(0, presentation="worn"), tp._img(1)])
)


def _converted(photo: bytes) -> bytes:
    """``photo`` on a mannequin, as the faked paste-back gives it."""
    r, g, b = hashlib.sha256(photo).digest()[:3]
    return png(48, 64, color=(r, g, b))


@pytest.fixture
def pasted(monkeypatch, mannequin_on) -> list[tuple[bytes, bytes]]:
    """The paste-back faked (``_converted``); each call's photo and edit."""
    calls = []

    def paste_back(photo: bytes, edit: bytes) -> bytes:
        calls.append((photo, edit))
        return _converted(photo)

    monkeypatch.setattr(mannequin, "paste_back", paste_back)
    return calls


def _images(fail: int = 0):
    """A mannequin edit answers ``EDIT`` (the first ``fail`` raise), a
    drawing a generated image."""
    drawings, left = tp._images_counter(), [fail]

    def respond(parts, *, system=None, params=None):
        if parts[0] != mannequin.PROMPT:
            return drawings(parts, system=system, params=params)
        if left[0]:
            left[0] -= 1
            raise GenerationError("no image in the answer")
        return EDIT

    return respond


def _edits(fake_gen) -> list[dict]:
    return [
        c
        for c in tp._image_calls(fake_gen)
        if c["raw_parts"][0] == mannequin.PROMPT
    ]


def _drawings(fake_gen) -> list[dict]:
    return [
        c
        for c in tp._image_calls(fake_gen)
        if c["raw_parts"][0] != mannequin.PROMPT
    ]


def test_a_worn_photo_reaches_every_step_on_a_mannequin(
    fake_gen, fakes, pasted, images, person
):
    fake_gen.json(tp._json_router(SINGLE))
    fake_gen.image(_images())
    events = tp._run(tp._request(images, person, 2))
    on = _converted(images[0])
    # One edit of the worn photo, its aspect ratio kept, then pasted back.
    (edit,) = _edits(fake_gen)
    assert edit["raw_parts"] == [mannequin.PROMPT, images[0]]
    assert edit["params"] == ImageParams(aspect_ratio="auto")
    assert pasted == [(images[0], EDIT)]
    # The close-up search, the drawing and the judge get it; the flat photo
    # and the person stay as they are.
    assert fakes.searched == [[on, images[1]]]
    drawing = _drawings(fake_gen)[0]
    assert tp._byte_parts(drawing) == [person, tp.FACE_CROP, on, images[1]]
    assert fakes.product_calls[0]["references"] == [on]
    c = events[0].classification
    assert [r.mannequin for r in c.products[0].refs] == [True, False]
    assert [p.mannequin for p in c.photos] == [True, False]
    text = tp._text_parts(drawing)
    assert "PRODUCT A top item (photo on a mannequin, front view)" in text
    assert "Take ONLY the top item from it." in text
    assert f"A — top item: worn untucked; {styling.FIT_MANNEQUIN}." in text


def test_an_outfit_and_its_look_photo_are_drawn_on_a_mannequin(
    fake_gen, fakes, pasted, images, person
):
    model = png(20, 40, color=(9, 9, 9))
    answer = tp._classified(
        ("A", "top", [tp._img(0, presentation="worn"), tp._look(2)]),
        ("B", "bottom", [tp._img(1)]),
    )
    fake_gen.json(tp._json_router(answer))
    fake_gen.image(_images())
    tp._run(tp._request(images, person, 2, reference_model_image=model))
    edited = sorted(e["raw_parts"][1] for e in _edits(fake_gen))
    assert edited == sorted([images[0], model])  # never the flat B
    # The look photo is boxed on a mannequin; the cards are drawn from it.
    assert fakes.crop_calls == [_converted(model)]
    drawing = _drawings(fake_gen)[0]
    sent = (_converted(images[0]), images[1], tp.CROPS[0], tp.CROPS[2])
    assert tp._byte_parts(drawing)[2:] == [tp._drawn(x) for x in sent]
    text = tp._text_parts(drawing)
    assert (
        "- image_2.png — PRODUCT A (top item), shown on a mannequin: take "
        "ONLY the top item from it; worn as in its REFERENCE CROP." in text
    )
    assert (
        "- image_4.png — REFERENCE CROP: PRODUCT A top item as worn on a "
        "mannequin, cropped from a photo of it; use it only for how it is "
        "worn and for its details." in text
    )
    assert "another person" not in text
    assert "different person" not in text


def test_a_photo_whose_edit_fails_twice_stays_as_it_is(
    fake_gen, fakes, pasted, images, person
):
    fake_gen.json(tp._json_router(SINGLE))
    fake_gen.image(_images(fail=2))
    events = tp._run(tp._request(images, person, 2))
    assert len(_edits(fake_gen)) == 2
    assert pasted == []
    drawing = _drawings(fake_gen)[0]
    assert tp._byte_parts(drawing)[2:] == images[:2]
    refs = events[0].classification.products[0].refs
    assert [r.mannequin for r in refs] == [False, False]
    text = tp._text_parts(drawing)
    assert "(photo on a person, front view)" in text
    assert skin.ONLY.format(what="top item") in text


def test_a_second_try_that_works_converts_the_photo(
    fake_gen, fakes, pasted, images, person
):
    fake_gen.json(tp._json_router(SINGLE))
    fake_gen.image(_images(fail=1))
    tp._run(tp._request(images, person, 2))
    assert len(_edits(fake_gen)) == 2
    assert tp._byte_parts(_drawings(fake_gen)[0])[2] == _converted(images[0])


def test_a_photo_that_cannot_be_converted_is_a_fallback(
    fake_gen, mannequin_on, images, person
):
    fake_gen.image(GenerationError("no image in the answer"))
    photo = images[0]
    ref = ProductRef(photo, "front", presentation="worn")
    c = Classification(
        "single",
        [Product("A", "top", "top", [ref])],
        "upper_body",
        photos=[Photo("A", "front", worn=True, zoom=False)],
    )
    req = TryOnRequest([photo], person)
    with degraded.tracking() as marker:
        kept = asyncio.run(worn.normalise(req, c))
    assert kept.product_images[0] is photo and ref.image is photo
    assert marker.reasons == ["mannequin photo"]
    assert not ref.mannequin and not c.photos[0].mannequin


def test_with_the_cache_off_every_run_converts_again(
    fake_gen, fakes, pasted, images, person
):
    fake_gen.json(tp._json_router(SINGLE))
    fake_gen.image(_images())
    req = tp._request(images, person, 2)
    tp._run(req)
    tp._run(req)
    assert len(_edits(fake_gen)) == 2


def test_a_converted_photo_is_kept_and_an_analysis_hit_uses_it(
    fake_gen, fakes, store, pasted, images, person
):
    fake_gen.json(tp._json_router(SINGLE))
    fake_gen.image(_images())
    req = tc._request(images, person, 2)
    tc._run(req)
    assert len(_edits(fake_gen)) == 1
    assert len(tc._indexes(store, "mannequin")) == 1
    # The analysis and the conversion both come from the cache, and the
    # drawing gets the same mannequin photo and wording as the first run.
    tc._run(tc._again(req))
    assert len(_edits(fake_gen)) == 1
    assert tc._classifier(fake_gen) == 1
    first, again = _drawings(fake_gen)
    assert tp._byte_parts(again)[2] == _converted(images[0])
    assert tp._byte_parts(again) == tp._byte_parts(first)
    assert tp._text_parts(again) == tp._text_parts(first)
    # A refresh converts it again.
    tc._run(tc._again(req), refresh=True)
    assert len(_edits(fake_gen)) == 2
