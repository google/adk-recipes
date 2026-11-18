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

"""The try-on prompt's face upscaled (``GENMEDIA_UPSCALE_FACE``): the
model, media and eval calls faked as in ``test_pipeline``, the cache over
the fake bucket as in ``test_cache``."""

from genmedia4commerce.core import media, settings
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows.tryon import inputs
from tests.workflows.tryon import test_cache as tc
from tests.workflows.tryon import test_pipeline as tp

fakes = tp.fakes
store = tc.store
FACE_2K = png(32, 32, color=(40, 50, 60))  # the image model's upscale
LARGE_FACE = png(1024, 1100, color=(4, 5, 6))  # a crop already large


def _upscale_calls(fake_gen) -> list[dict]:
    return [
        c
        for c in tp._image_calls(fake_gen)
        if c["raw_parts"][0] == media.FACE_UPSCALE_PROMPT
    ]


def _drawings(fake_gen) -> list[dict]:
    return [
        c
        for c in tp._image_calls(fake_gen)
        if c["raw_parts"][0] != media.FACE_UPSCALE_PROMPT
    ]


def _image_model(upscaled=FACE_2K):
    """The image model: the upscale gives ``upscaled`` (raised when an
    exception), a drawing one of ``tp.GENERATED``."""
    draw = tp._images_counter()

    def respond(parts, *, system=None, params=None):
        if parts[0] != media.FACE_UPSCALE_PROMPT:
            return draw(parts, system=system, params=params)
        if isinstance(upscaled, BaseException):
            raise upscaled
        return upscaled

    return respond


def _upscale_face(monkeypatch, on: bool = True) -> None:
    monkeypatch.setenv("GENMEDIA_UPSCALE_FACE", "true" if on else "false")
    settings.get_settings.cache_clear()


def _single(fake_gen, images, person, upscaled=FACE_2K):
    answer = tp._classified(("A", "headwear", [tp._img(0)]))
    fake_gen.json(tp._json_router(answer))
    fake_gen.image(_image_model(upscaled))
    return tc._request(images, person, 1)


def test_off_by_default_the_crop_is_sent_as_it_is(
    fake_gen, fakes, images, person
):
    tp._run(_single(fake_gen, images, person))
    assert _upscale_calls(fake_gen) == []
    drawn = tp._byte_parts(_drawings(fake_gen)[0])
    assert drawn == [person, tp.FACE_CROP, images[0]]


def test_on_the_prompt_gets_the_upscaled_face_and_the_eval_the_photos(
    fake_gen, fakes, images, person, monkeypatch
):
    _upscale_face(monkeypatch)
    events = tp._run(_single(fake_gen, images, person))
    assert events[-1].type == "complete"
    (call,) = _upscale_calls(fake_gen)
    assert call["raw_parts"] == [media.FACE_UPSCALE_PROMPT, tp.FACE_CROP]
    params = call["params"]
    assert (params.image_size, params.aspect_ratio) == ("2K", "auto")
    for drawing in _drawings(fake_gen):
        assert tp._byte_parts(drawing) == [person, FACE_2K, images[0]]
    assert fakes.faces  # the judge sees the photo's crop
    assert all(face == tp.FACE_CROP for face in fakes.faces)


def test_no_face_no_upscale(fake_gen, fakes, images, person, monkeypatch):
    _upscale_face(monkeypatch)
    fakes.face_box = None
    tp._run(_single(fake_gen, images, person))
    assert _upscale_calls(fake_gen) == []
    assert tp._byte_parts(_drawings(fake_gen)[0]) == [person, images[0]]


def test_a_large_crop_is_not_upscaled(
    fake_gen, fakes, images, person, monkeypatch
):
    _upscale_face(monkeypatch)
    cut = media.crop_box

    def crop_box(data, box, **kw):
        return LARGE_FACE if box == tp.FACE_BOX else cut(data, box, **kw)

    monkeypatch.setattr(media, "crop_box", crop_box)
    assert min(media.image_size(LARGE_FACE)) == inputs.FACE_MIN_SIDE
    tp._run(_single(fake_gen, images, person))
    assert _upscale_calls(fake_gen) == []
    assert tp._byte_parts(_drawings(fake_gen)[0])[1] == LARGE_FACE


def test_a_failed_upscale_keeps_the_crop(
    fake_gen, fakes, store, images, person, monkeypatch, caplog
):
    _upscale_face(monkeypatch)
    down = GenerationError("image model down")
    events = tc._run(_single(fake_gen, images, person, upscaled=down))
    assert events[-1].type == "complete"
    assert len(_upscale_calls(fake_gen)) == 1
    drawn = tp._byte_parts(_drawings(fake_gen)[0])
    assert drawn == [person, tp.FACE_CROP, images[0]]
    assert "face upscale failed" in caplog.text
    assert len(tc._indexes(store, "analysis")) == 1  # not about the person


def test_a_product_hit_still_cuts_and_upscales_the_face_again(
    fake_gen, fakes, store, images, person, monkeypatch
):
    _upscale_face(monkeypatch)
    req = _single(fake_gen, images, person)
    tc._run(req)
    tc._run(tc._again(req))
    assert tc._classifier(fake_gen) == 1  # the products' analysis was read
    assert len(fakes.faces_cut) == 2
    assert len(_upscale_calls(fake_gen)) == 2
    assert FACE_2K not in store.uploads.values()
    last = tp._byte_parts(_drawings(fake_gen)[-1])
    assert last == [person, FACE_2K, images[0]]
