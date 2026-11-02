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

"""The photo try-on's preprocessing cache, end to end over a fake bucket
(the model, media and eval calls faked as in ``test_pipeline``)."""

import asyncio
import json
import time

import pytest

from genmedia4commerce.core import cache as core_cache
from genmedia4commerce.core import gcs, media, models, runs, settings
from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.events import ClassifiedEvent, RecordEvent
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.studio import media as studio_media
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.tryon import (
    cache,
    closeups,
    pipeline,
    reference,
)
from genmedia4commerce.workflows.tryon.types import TryOnRequest
from tests.workflows.tryon import test_pipeline as tp

fakes = tp.fakes  # the faked media, eval and look-photo calls
REAL_UPSCALE_SHOES = reference.upscale_shoes
REAL_FIND = details.find
MODEL_PHOTO = png(20, 40, color=(9, 9, 9))


def _copy(data: bytes | None) -> bytes | None:
    """Equal bytes in a new object (a later request's own photo)."""
    return None if data is None else bytes(bytearray(data))


@pytest.fixture
def store(storage, monkeypatch, fakes):
    """The cache on, over the fake bucket; downloads give new objects (as
    a real read does) and are counted; Vision calls are counted."""
    monkeypatch.setenv("GENMEDIA_USE_CACHE", "true")
    settings.get_settings.cache_clear()
    real_download = storage.download
    storage.reads = []

    def download(uri: str) -> bytes:
        storage.reads.append(uri)
        return bytes(bytearray(real_download(uri)))  # a new object

    monkeypatch.setattr(gcs, "download", download)
    storage.vision = []
    face = media.face_box

    def face_box(data):
        storage.vision.append(data)
        return face(data)

    monkeypatch.setattr(media, "face_box", face_box)
    return storage


def _run(req: TryOnRequest, *, refresh: bool = False) -> list:
    """The run's events, once its background cache writes are over."""

    async def collect():
        with core_cache.refreshing(refresh):
            events = [e async for e in pipeline.run_tryon(req)]
        await asyncio.gather(*list(core_cache._WRITES))
        return events

    return asyncio.run(collect())


def _request(images, person, n, **kw) -> TryOnRequest:
    kw.setdefault("policy", RunPolicy(num_variations=1))
    return tp._request(images, person, n, **kw)


def _again(req: TryOnRequest, **changes) -> TryOnRequest:
    """``req`` again with photos in new objects (a later request)."""
    fields = {
        "product_images": [_copy(p) for p in req.product_images],
        "person_image": _copy(req.person_image),
        "reference_model_image": _copy(req.reference_model_image),
        "complete_look": req.complete_look,
        "upscale_crops": req.upscale_crops,
        "style_guidelines": req.style_guidelines,
        "policy": req.policy,
    }
    return TryOnRequest(**{**fields, **changes})


def _calls(fake_gen, role: str) -> int:
    """Model calls whose system prompt names ``role``."""
    return sum(role in (c.get("system") or "") for c in fake_gen.calls)


def _classifier(fake_gen) -> int:
    return _calls(fake_gen, "product classifier")


def _planner(fake_gen) -> int:
    return _calls(fake_gen, "fashion stylist")


def _cached(storage) -> list[str]:
    """The cache's object paths in the media bucket."""
    prefix = f"gs://{settings.get_settings().media_bucket}/"
    return sorted(
        uri.removeprefix(prefix)
        for uri in storage.objects
        if uri.startswith(prefix + "product_cache/")
    )


def _indexes(storage, name: str) -> list[str]:
    return [p for p in _cached(storage) if p.endswith(f"/{name}.json")]


def _index(storage, path: str) -> dict:
    return json.loads(storage.objects[storage.media(path)])


def _of(events, kind):
    return next(e for e in events if isinstance(e, kind))


def _outfit(fake_gen, fakes, images, person, **kw) -> TryOnRequest:
    """An outfit with a reference model adding shoes (upscaled), a detail
    found in the top's photo and a styling plan."""
    fakes.boxes = {"lace-up shoes": tp.SHOES}
    fakes.found = {images[0]: [tp.STAMP]}
    fake_gen.json(tp._json_router(tp.TOP_BOTTOM_LOOK))
    fake_gen.image(tp._images_counter())
    kw.setdefault("reference_model_image", MODEL_PHOTO)
    return _request(images, person, 2, upscale_crops=True, **kw)


def _single(fake_gen, images, person, **kw) -> TryOnRequest:
    answer = tp._classified(("A", "top", [tp._img(0), tp._img(1)]))
    fake_gen.json(tp._json_router(answer))
    fake_gen.image(tp._images_counter())
    return _request(images, person, 2, **kw)


def test_the_same_inputs_again_are_a_hit_with_no_preprocessing_call(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    fresh = _run(req)
    names = sorted(p.rsplit("/", 1)[-1] for p in _cached(store))
    indexes = [n for n in names if n.endswith(".json")]
    assert indexes[0] == "analysis.json"
    assert len(indexes) == 2 and indexes[1].startswith("plan-")
    counts = (
        _classifier(fake_gen),
        _planner(fake_gen),
        len(fakes.crop_calls),
        len(fakes.searched),
        len(fakes.upscaled),
    )
    assert counts == (1, 1, 1, 2, 1)  # a search per product
    first_drawing = len(tp._image_calls(fake_gen))
    written = dict(store.uploads)

    hit = _run(_again(req))
    assert (
        _classifier(fake_gen),
        _planner(fake_gen),
        len(fakes.crop_calls),
        len(fakes.searched),
        len(fakes.upscaled),
    ) == counts
    assert store.uploads == written  # nothing written again
    # The hit is rebuilt around this request's own photos: its classified
    # event, run record and drawing are the fresh run's.
    assert [e.type for e in hit] == [e.type for e in fresh]
    assert (
        _of(hit, ClassifiedEvent).payload == _of(fresh, ClassifiedEvent).payload
    )
    # the inventory and the heroes come back too
    kept = _of(hit, ClassifiedEvent).classification
    made = _of(fresh, ClassifiedEvent).classification
    assert kept.photos == made.photos and kept.look_photo == 2
    assert [i.name for i in kept.photos[2].items] == ["lace-up shoes"]
    assert [p.refs[0].source for p in kept.products] == [
        "upload",
        "upload",
        "model_crop",
    ]
    fresh_record, hit_record = _of(fresh, RecordEvent), _of(hit, RecordEvent)
    assert hit_record.payload == fresh_record.payload
    assert hit_record.files == fresh_record.files
    drawings = tp._image_calls(fake_gen)
    first, again = drawings[0], drawings[first_drawing]
    assert tp._byte_parts(again) == tp._byte_parts(first)
    assert again["system"] == first["system"]
    assert tp._text_parts(again) == tp._text_parts(first)


def test_a_hit_names_this_requests_own_photos(
    fake_gen, fakes, store, images, person
):
    req = _single(fake_gen, images, person)
    _run(req)
    later = _again(req)
    (classified,) = [e for e in _run(later) if e.type == "classified"]
    refs = classified.classification.products[0].refs
    assert refs[0].image is later.product_images[0]
    assert refs[1].image is later.product_images[1]
    assert _classifier(fake_gen) == 1


def _closeups_off(monkeypatch):
    monkeypatch.setenv("GENMEDIA_TRYON_CLOSEUPS", "false")
    settings.get_settings.cache_clear()


def _other_code(monkeypatch):
    monkeypatch.setattr(core_cache, "code_fingerprint", lambda: "other")


def _other_model(name):
    def change(monkeypatch):
        monkeypatch.setenv(name, "another-model")
        models.get_models.cache_clear()

    return change


_CHANGES = {
    "another photo": ({"photos": [0, 2]}, None),
    "another order": ({"photos": [1, 0]}, None),
    "a look photo": ({"reference_model_image": MODEL_PHOTO}, None),
    "complete_look": ({"complete_look": False}, None),
    "upscale_crops": ({"upscale_crops": True}, None),
    "closeups switch": ({}, _closeups_off),
    "code": ({}, _other_code),
    "judge model": ({}, _other_model("GENMEDIA_JUDGE_MODEL")),
    "image model": ({}, _other_model("GENMEDIA_IMAGE_MODEL")),
}


@pytest.mark.parametrize("change", list(_CHANGES))
def test_anything_the_analysis_depends_on_changed_is_a_miss(
    fake_gen, fakes, store, images, person, monkeypatch, change
):
    req = _single(fake_gen, images, person)
    _run(req)
    fields, environment = _CHANGES[change]
    fields = dict(fields)
    photos = fields.pop("photos", [0, 1])
    if environment:
        environment(monkeypatch)
    later = _again(req, **fields)
    later.product_images = [_copy(images[i]) for i in photos]
    _run(later)
    assert _classifier(fake_gen) == 2
    assert len(_indexes(store, "analysis")) == 2


def test_nothing_from_the_person_photo_is_kept(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    _run(req)
    _run(_again(req))
    assert _classifier(fake_gen) == 1  # the products' analysis was a hit
    # the person photo is prepared again on every run
    assert (len(store.vision), len(fakes.faces_cut)) == (2, 2)
    written = store.uploads
    assert written and all("/product_cache/" in uri for uri in written)
    assert not {person, tp.FACE_CROP} & set(written.values())


def test_new_styling_rules_recompute_only_the_plan(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person, style_guidelines="a")
    _run(req)
    _run(_again(req, style_guidelines="b"))
    assert (_classifier(fake_gen), _planner(fake_gen)) == (1, 2)
    plans = [p for p in _cached(store) if "/plan-" in p]
    assert len(plans) == 2
    entry = _index(store, _indexes(store, "analysis")[0])["entry"]
    assert {_index(store, p)["analysis"] for p in plans} == {entry}
    _run(_again(req, style_guidelines="b"))
    _run(_again(req, style_guidelines="a"))
    assert (_classifier(fake_gen), _planner(fake_gen)) == (1, 2)


def _judge_down(*args, **kwargs):
    raise GenerationError("judge down")


@pytest.mark.parametrize(
    "broken",
    [
        (details, "_found", REAL_FIND),  # caught in details.find
        (details, "find", _judge_down),  # caught in closeups._search_one
        (closeups, "_search_one", _judge_down),  # caught in closeups.wait
    ],
    ids=["find", "product", "wait"],
)
def test_a_failed_close_up_search_keeps_no_analysis(
    fake_gen, fakes, store, images, person, monkeypatch, broken
):
    module, name, find = broken
    monkeypatch.setattr(details, "find", find)
    monkeypatch.setattr(module, name, _judge_down)
    events = _run(_single(fake_gen, images, person))
    assert events[-1].type == "complete"  # drawn without close-ups
    assert _indexes(store, "analysis") == []


def test_a_failed_shoe_upscale_keeps_no_analysis(
    fake_gen, fakes, store, images, person, monkeypatch
):
    def upscale(data, **kw):
        raise GenerationError("image model down")

    monkeypatch.setattr(reference, "upscale_shoes", REAL_UPSCALE_SHOES)
    monkeypatch.setattr(media, "upscale", upscale)
    events = _run(_outfit(fake_gen, fakes, images, person))
    assert events[-1].type == "complete"  # drawn with the plain crop
    assert _indexes(store, "analysis") == []
    assert [p for p in _cached(store) if "/plan-" in p] == []


def test_a_failed_styling_plan_is_not_kept(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    route = tp._json_router(tp.TOP_BOTTOM_LOOK)

    def respond(parts, *, system=None, schema=None, params=None):
        if "fashion stylist" in (system or ""):
            raise GenerationError("planner down")
        return route(parts, system=system, schema=schema, params=params)

    fake_gen.json(respond)
    _run(req)
    assert [p for p in _cached(store) if "/plan-" in p] == []
    assert len(_indexes(store, "analysis")) == 1


def test_a_vision_error_fails_the_run_and_keeps_nothing(
    fake_gen, fakes, store, images, person, monkeypatch
):
    def face_box(data):
        raise RuntimeError("Vision unavailable")

    monkeypatch.setattr(media, "face_box", face_box)
    events = _run(_single(fake_gen, images, person))
    assert events[-1].type == "error"
    assert _cached(store) == []


def test_a_cancelled_run_keeps_nothing(fake_gen, fakes, store, images, person):
    req = _single(fake_gen, images, person)

    async def first_event():
        stream = pipeline.run_tryon(req)
        event = await anext(stream)
        await stream.aclose()
        await asyncio.gather(*list(core_cache._WRITES))
        return event

    assert asyncio.run(first_event()).type == "classified"
    assert _cached(store) == []


def test_a_timed_out_run_keeps_nothing(
    fake_gen, fakes, store, images, person, monkeypatch
):
    def slow(images, **kw):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(details, "find", slow)
    policy = RunPolicy(num_variations=1, deadline_s=0.2)
    events = _run(_single(fake_gen, images, person, policy=policy))
    assert events[-1].type == "error"
    assert _cached(store) == []


def test_blobs_without_their_index_are_a_miss(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    _run(req)
    (path,) = _indexes(store, "analysis")
    store.delete(store.media(path))  # blobs left, no index
    _run(_again(req))
    assert _classifier(fake_gen) == 2
    assert _indexes(store, "analysis") == [path]
    _run(_again(req))
    assert _classifier(fake_gen) == 2


def test_an_index_with_a_missing_blob_is_a_miss_and_replaced(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    _run(req)
    (path,) = _indexes(store, "analysis")
    before = _index(store, path)
    folder = path.rsplit("/", 1)[0]
    store.delete(
        store.media(f"{folder}/{next(iter(before['blobs'].values()))}")
    )
    _run(_again(req))
    assert _classifier(fake_gen) == 2
    assert _index(store, path)["entry"] != before["entry"]  # replaced
    _run(_again(req))
    assert _classifier(fake_gen) == 2


@pytest.mark.parametrize("field", ["version", "key"])
def test_an_index_of_another_version_or_key_is_a_miss(
    fake_gen, fakes, store, images, person, field
):
    req = _single(fake_gen, images, person)
    _run(req)
    (path,) = _indexes(store, "analysis")
    index = {**_index(store, path), field: "other"}
    store.objects[store.media(path)] = json.dumps(index).encode()
    _run(_again(req))
    assert _classifier(fake_gen) == 2


def test_a_hit_keeps_the_garments_span_and_older_entries_are_a_miss(
    fake_gen, fakes, store, images, person
):
    """Version 2 entries hold the span (``Product.span``); an entry of the
    format before it (version 1, no span) is recomputed."""
    req = _single(fake_gen, images, person)
    fresh = _run(req)
    hit = _run(_again(req))
    assert _classifier(fake_gen) == 1
    for events in (fresh, hit):
        (top,) = _of(events, ClassifiedEvent).classification.products
        assert top.span == ("shoulders", "hips")
    framed = "From the top of the head to mid-thigh."
    assert all(framed in tp._text_parts(c) for c in tp._image_calls(fake_gen))
    assert core_cache.VERSION == 2
    (path,) = _indexes(store, "analysis")
    index = {**_index(store, path), "version": 1}
    store.objects[store.media(path)] = json.dumps(index).encode()
    _run(_again(req))
    assert _classifier(fake_gen) == 2


def test_try_again_recomputes_and_replaces_every_entry(
    fake_gen, fakes, store, images, person
):
    req = _outfit(fake_gen, fakes, images, person)
    _run(req)
    paths = [p for p in _cached(store) if p.endswith(".json")]
    before = {p: _index(store, p)["entry"] for p in paths}
    _run(_again(req), refresh=True)
    assert (_classifier(fake_gen), _planner(fake_gen)) == (2, 2)
    assert len(fakes.searched) == 4
    after = {p: _index(store, p)["entry"] for p in paths}
    assert all(after[p] != before[p] for p in paths)  # all replaced
    analysis = after[next(p for p in paths if p.endswith("/analysis.json"))]
    plan = next(p for p in paths if "/plan-" in p)
    assert _index(store, plan)["analysis"] == analysis
    _run(_again(req))
    assert (_classifier(fake_gen), _planner(fake_gen)) == (2, 2)


def test_with_the_cache_off_nothing_is_read_or_written(
    fake_gen, fakes, store, images, person, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_USE_CACHE", "false")
    settings.get_settings.cache_clear()
    req = _outfit(fake_gen, fakes, images, person)
    _run(req)
    _run(_again(req))
    assert (store.reads, store.uploads) == ([], {})
    assert _classifier(fake_gen) == 2


def test_cache_objects_are_never_served_or_accepted_as_images(
    fake_gen, fakes, store, images, person
):
    _run(_outfit(fake_gen, fakes, images, person))
    paths = _cached(store)
    assert paths
    for path in paths:
        uri = store.media(path)
        assert not runs.is_media_file(path)
        assert not studio_media.allowed_ref(uri)
        with pytest.raises(InputError):
            gcs.resolve_image(uri)


def test_a_cache_write_that_fails_only_logs(
    fake_gen, fakes, store, images, person, monkeypatch, caplog
):
    def broken(*args, **kwargs):
        raise OSError("bucket unavailable")

    monkeypatch.setattr(gcs, "upload", broken)
    monkeypatch.setattr(gcs, "create", broken)
    events = _run(_outfit(fake_gen, fakes, images, person))
    assert events[-1].type == "complete"
    assert _cached(store) == []
    assert "could not store" in caplog.text


# ---- video try-on: classifying its own photos ----------------------------


def _classified_by(calls: list, upscaled: bool = False):
    async def classify(photos, look, complete_look, *, upscale_crops=False):
        calls.append(photos)
        if upscaled:
            reference.upscale_shoes(b"crop")
        ref = ProductRef(photos[0], "front")
        return Classification(
            "single", [Product("A", "top", "t", [ref])], "upper_body"
        ), {}

    return classify


def test_video_classification_is_kept_for_the_same_photos(
    store, images, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(reference, "classify", _classified_by(calls))

    async def twice():
        first = await cache.classify(images[:2], None, True)
        await asyncio.gather(*list(core_cache._WRITES))
        later = [_copy(p) for p in images[:2]]
        again = await cache.classify(later, None, True)
        return first, again, later

    first, again, later = asyncio.run(twice())
    assert len(calls) == 1
    assert again[0].to_dict() == first[0].to_dict()
    assert again[0].products[0].refs[0].image is later[0]


def test_video_classification_with_a_fallback_is_not_kept(
    store, images, monkeypatch
):
    calls: list = []
    monkeypatch.setattr(reference, "classify", _classified_by(calls, True))
    monkeypatch.setattr(reference, "upscale_shoes", REAL_UPSCALE_SHOES)

    def upscale(data, **kw):
        raise GenerationError("image model down")

    monkeypatch.setattr(media, "upscale", upscale)

    async def twice():
        for _ in range(2):
            await cache.classify(images[:2], None, True)
            await asyncio.gather(*list(core_cache._WRITES))

    asyncio.run(twice())
    assert len(calls) == 2
    assert _cached(store) == []
