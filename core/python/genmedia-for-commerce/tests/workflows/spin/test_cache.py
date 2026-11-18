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

"""The 360 spin's preprocessing cache, end to end over a fake bucket: the
real classification, references and close-ups on faked model calls; the
videos and their checks faked."""

import asyncio
import json

import pytest

from genmedia4commerce.core import cache as core_cache
from genmedia4commerce.core import gcs, models, runs, settings
from genmedia4commerce.core.errors import GenerationError, InputError
from genmedia4commerce.core.runner import RunPolicy
from genmedia4commerce.core.testing import mp4_stub, png
from genmedia4commerce.core.types import Verdict
from genmedia4commerce.studio import media as studio_media
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.spin import classify, product_refs
from genmedia4commerce.workflows.spin import pipeline as spin_pipeline
from genmedia4commerce.workflows.spin.pipeline import run_spin
from genmedia4commerce.workflows.spin.types import SpinRequest
from tests.workflows.spin import test_product_refs as pr
from tests.workflows.spin import test_shoe_refs as sr

pytestmark = pytest.mark.asyncio

CLIP = mp4_stub(frames=12)
BACK = png(300, 300, color=(10, 20, 30))  # a flat photo, used as it is
UPSCALED = png(512, 512, color=(1, 2, 3))
PLAQUE = {
    "kind": "text",
    "text": "Velmora",
    "legible": True,
    "photo": 1,
    "box_2d": [100, 100, 600, 600],
    "side": "back",
    "where": "plaque on the back",
    "name": "plaque",
}


def _copy(data: bytes) -> bytes:
    """Equal bytes in a new object (a later request's own photo)."""
    return bytes(bytearray(data))


@pytest.fixture
def store(storage, monkeypatch, fake_gen):
    """The cache and close-ups on, over the fake bucket; downloads give new
    objects (as a real read does) and are counted. Videos and their checks
    are faked."""
    monkeypatch.setenv("GENMEDIA_USE_CACHE", "true")
    monkeypatch.setenv("GENMEDIA_SPIN_CLOSEUPS", "true")
    settings.get_settings.cache_clear()
    real_download = storage.download
    storage.reads = []

    def download(uri: str) -> bytes:
        storage.reads.append(uri)
        return _copy(real_download(uri))

    monkeypatch.setattr(gcs, "download", download)
    fake_gen.video(CLIP)
    monkeypatch.setattr(spin_pipeline, "_half_problem", lambda half: None)
    monkeypatch.setattr(
        spin_pipeline,
        "_evaluate",
        lambda clip, refs, route: (Verdict(True), clip),
    )
    return storage


def _router(product_type: str, answers: dict, judge=None):
    """The judge model: the classification (one entry per photo in
    ``answers['views']``), the cut by name, the close-up search; any other
    call goes to ``judge`` (the shoe calls)."""

    def respond(parts, *, system=None, schema=None, params=None):
        if schema is classify._SCHEMA:
            views = answers["views"]
            return {
                "product_type": product_type,
                "description": "a red handbag",
                "classifications": [
                    {"index": i, "view": v, "rank": 1, "presentation": p}
                    for i, (v, p) in enumerate(views)
                ],
            }
        if schema is product_refs._CUT_SCHEMA:
            return answers["cut"]
        if schema is details._SCHEMA:
            return {"details": [PLAQUE]}
        assert judge is not None, f"unexpected call: {parts[:1]}"
        return judge(parts, system=system, schema=schema, params=params)

    return respond


@pytest.fixture
def handbag(fake_gen, store):
    """A worn front (cut out by name) and a flat back (used as it is), a
    close-up found and upscaled."""
    answers = {
        "views": [("front", "worn"), ("back", "flat")],
        "cut": {"boxes": [pr.outline(40, 80, 90, 170)]},
    }
    fake_gen.json(_router("other", answers))
    fake_gen.image(UPSCALED)
    answers["photos"] = [pr.worn_photo(), BACK]
    return answers


@pytest.fixture
def pair(fake_gen, store, judge):
    """Two right side views shown on opposite sides: the left-foot one is
    mirrored by the image model."""
    judge.labels = ["right", "right"]
    judge.feet = {sr.BLUE: "left"}
    judge.pairs = {frozenset({sr.RED, sr.BLUE}): "opposite_sides"}
    answers = {"views": [("right", "flat"), ("right", "flat")]}
    fake_gen.json(_router("shoes", answers, judge))
    fake_gen.image(sr._mirror_of_input)
    return [sr._single(sr.RED), sr._lopsided(sr.BLUE)]


judge = sr.judge  # the shoe judge's answers (a fixture)


async def _run(photos, *, refresh: bool = False) -> list:
    """The run's events, once its background cache writes are over."""
    req = SpinRequest(list(photos), policy=RunPolicy(num_variations=1))
    with core_cache.refreshing(refresh):
        events = [e async for e in run_spin(req)]
    await asyncio.gather(*list(core_cache._WRITES))
    return events


def _prep(fake_gen) -> list[dict]:
    """The calls made before the videos."""
    return [c for c in fake_gen.calls if c["fn"] != "generate_video"]


def _classifier(fake_gen) -> int:
    return sum(c.get("schema") is classify._SCHEMA for c in fake_gen.calls)


def _videos(fake_gen) -> list[dict]:
    return [c for c in fake_gen.calls if c["fn"] == "generate_video"]


def _cached(storage) -> list[str]:
    prefix = f"gs://{settings.get_settings().media_bucket}/"
    return sorted(
        uri.removeprefix(prefix)
        for uri in storage.objects
        if uri.startswith(prefix + "spin_cache/")
    )


def _indexes(storage) -> list[str]:
    return [p for p in _cached(storage) if p.endswith("/prepared.json")]


async def test_a_product_spin_again_makes_no_preprocessing_call(
    fake_gen, store, handbag
):
    photos = handbag["photos"]
    fresh = await _run(photos)
    assert fresh[-1].type == "complete"
    made = _prep(fake_gen)
    kinds = sorted(c["fn"] for c in made)
    assert kinds == ["generate_image"] + ["generate_json"] * 3
    assert len(_indexes(store)) == 1
    assert store.uploads and all("/spin_cache/" in u for u in store.uploads)
    assert not set(photos) & set(store.uploads.values())  # named by position
    written = dict(store.uploads)

    later = [_copy(p) for p in photos]
    hit = await _run(later)
    assert _prep(fake_gen) == made  # no classification, cut, search, upscale
    assert store.uploads == written
    assert [e.type for e in hit] == [e.type for e in fresh]
    assert hit[0].payload == fresh[0].payload  # the classified event
    first, again = _videos(fake_gen)[0], _videos(fake_gen)[2]
    assert again["prompt"] == first["prompt"]
    assert again["raw_parts"] == first["raw_parts"]
    assert again["raw_parts"][1] is later[1]  # this request's own photo
    assert again["raw_parts"][2] == UPSCALED  # the close-up


async def test_a_shoe_spin_again_makes_no_labelling_or_mirroring_call(
    fake_gen, store, pair
):
    await _run(pair)
    made = _prep(fake_gen)
    assert [c["fn"] for c in made].count("generate_image") == 1  # the twin
    await _run([_copy(p) for p in pair])
    assert _prep(fake_gen) == made
    first, again = _videos(fake_gen)
    assert again["prompt"] == first["prompt"]
    assert "generated mirror image" in again["prompt"]  # named, as before
    assert again["raw_parts"] == first["raw_parts"]


def _other_code(monkeypatch):
    monkeypatch.setattr(core_cache, "code_fingerprint", lambda: "other")


def _closeups_off(monkeypatch):
    monkeypatch.setenv("GENMEDIA_SPIN_CLOSEUPS", "false")
    settings.get_settings.cache_clear()


def _other_model(name):
    def change(monkeypatch):
        monkeypatch.setenv(name, "another-model")
        models.get_models.cache_clear()

    return change


_CHANGES = {
    "another photo": ([0, 0], None),
    "another order": ([1, 0], None),
    "code": ([0, 1], _other_code),
    "closeups switch": ([0, 1], _closeups_off),
    "judge model": ([0, 1], _other_model("GENMEDIA_JUDGE_MODEL")),
    "image model": ([0, 1], _other_model("GENMEDIA_IMAGE_MODEL")),
}


@pytest.mark.parametrize("change", list(_CHANGES))
async def test_anything_the_entry_depends_on_changed_is_a_miss(
    fake_gen, store, handbag, monkeypatch, change
):
    await _run(handbag["photos"])
    order, environment = _CHANGES[change]
    if environment:
        environment(monkeypatch)
    await _run([_copy(handbag["photos"][i]) for i in order])
    assert _classifier(fake_gen) == 2
    assert len(_indexes(store)) == 2


def _upscale_fails(fake_gen, handbag):
    fake_gen.image(GenerationError("image model down"))


def _cut_fails(fake_gen, handbag):
    handbag["cut"] = {"boxes": []}


def _photo_skipped(fake_gen, handbag):
    handbag["views"] = handbag["views"][:1]


@pytest.mark.parametrize(
    "fallback", [_upscale_fails, _cut_fails, _photo_skipped]
)
async def test_a_product_spin_with_a_fallback_keeps_nothing(
    fake_gen, store, handbag, fallback
):
    fallback(fake_gen, handbag)
    events = await _run(handbag["photos"])
    assert events[-1].type == "complete"  # made with less
    assert _cached(store) == []
    await _run(handbag["photos"])
    assert _classifier(fake_gen) == 2


async def test_a_shoe_twin_never_flipped_keeps_nothing(fake_gen, store, pair):
    fake_gen.image(lambda parts, **kw: parts[1])  # the input, unchanged
    events = await _run(pair)
    assert events[-1].type == "complete"  # without the mirrored view
    assert _cached(store) == []


async def test_a_failed_run_keeps_nothing(fake_gen, store, pair, judge):
    judge.labels = ["invalid", "invalid"]
    events = await _run(pair)
    assert events[-1].type == "error"
    assert _cached(store) == []


async def test_try_again_recomputes_and_replaces_the_entry(
    fake_gen, store, handbag
):
    await _run(handbag["photos"])
    (path,) = _indexes(store)
    before = json.loads(store.objects[store.media(path)])["entry"]
    await _run(handbag["photos"], refresh=True)
    assert _classifier(fake_gen) == 2
    after = json.loads(store.objects[store.media(path)])["entry"]
    assert after != before
    await _run(handbag["photos"])
    assert _classifier(fake_gen) == 2


async def test_with_the_cache_off_nothing_is_read_or_written(
    fake_gen, store, handbag, monkeypatch
):
    monkeypatch.setenv("GENMEDIA_USE_CACHE", "false")
    settings.get_settings.cache_clear()
    await _run(handbag["photos"])
    await _run(handbag["photos"])
    assert (store.reads, store.uploads) == ([], {})
    assert _classifier(fake_gen) == 2


async def test_cache_objects_are_never_served_or_accepted_as_images(
    store, handbag
):
    await _run(handbag["photos"])
    assert _cached(store)
    for path in _cached(store):
        uri = store.media(path)
        assert not runs.is_media_file(path)
        assert not studio_media.allowed_ref(uri)
        with pytest.raises(InputError):
            gcs.resolve_image(uri)
