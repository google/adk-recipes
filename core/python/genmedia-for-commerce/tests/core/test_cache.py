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

"""Tests for ``core.cache`` and ``core._codec`` over the fake bucket."""

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from genmedia4commerce.core import _codec, cache, gcs
from genmedia4commerce.core.testing import png

A, B, LOOK = png(color=(1, 2, 3)), png(color=(4, 5, 6)), png(color=(7, 8, 9))


@dataclass
class Part:
    image: bytes
    box: tuple[int, int] = (0, 0)


@dataclass
class Whole:
    parts: list[Part]
    named: dict[str, bytes] = field(default_factory=dict)
    note: str | None = None


def _whole(crop: bytes) -> Whole:
    return Whole([Part(A, (1, 2)), Part(crop), Part(crop)], {"crop": crop})


def test_codec_names_inputs_by_position_and_keeps_shared_images_shared():
    crop = png(color=(9, 9, 9))
    data, blobs = _codec.encode(_whole(crop), [A, LOOK])
    assert list(blobs.values()) == [crop]  # one blob, inputs not stored
    json.dumps(data)  # JSON-ready
    later = bytes(bytearray(A))
    downloaded = {k: bytes(bytearray(v)) for k, v in blobs.items()}
    back = _codec.decode(
        data, [later, LOOK], downloaded, {"Whole": Whole, "Part": Part}
    )
    assert back == _whole(crop)
    assert back.parts[0].image is later  # the caller's own input
    assert back.parts[0].box == (1, 2)
    assert back.parts[1].image is back.parts[2].image is back.named["crop"]


@pytest.mark.parametrize(
    ("data", "inputs"),
    [
        ({"$input": 3}, [A]),
        ({"$input": -1}, [A]),
        ({"$input": 1}, [A, None]),
        ({"$type": "Unknown", "fields": {}}, []),
        ({"$weird": 1}, []),
    ],
)
def test_codec_refuses_data_that_does_not_fit(data, inputs):
    with pytest.raises((ValueError, KeyError, IndexError)):
        _codec.decode(data, inputs, {}, {"Part": Part})


def test_codec_refuses_values_it_cannot_write():
    with pytest.raises(TypeError):
        _codec.encode({"x": object()})


def test_code_fingerprint_covers_code_and_prompts(tmp_path, monkeypatch):
    (tmp_path / "eval" / "prompts").mkdir(parents=True)
    (tmp_path / "a.py").write_text("x = 1")
    prompt = tmp_path / "eval" / "prompts" / "judge.md"
    prompt.write_text("Judge.")
    (tmp_path / "panel.html").write_text("<p>")
    monkeypatch.setattr(cache, "_PACKAGE", tmp_path)
    cache.code_fingerprint.cache_clear()
    try:
        first = cache.code_fingerprint()
        (tmp_path / "panel.html").write_text("<p>changed")
        cache.code_fingerprint.cache_clear()
        assert cache.code_fingerprint() == first  # not a source file
        prompt.write_text("Judge harder.")
        assert cache.code_fingerprint() == first  # once per process
        cache.code_fingerprint.cache_clear()
        assert cache.code_fingerprint() != first
    finally:
        cache.code_fingerprint.cache_clear()


def test_key_depends_on_every_part_and_the_code(monkeypatch):
    base = cache.key(kind="x", photos=["a", "b"])
    assert base == cache.key(photos=["a", "b"], kind="x")
    assert base != cache.key(kind="x", photos=["b", "a"])
    monkeypatch.setattr(cache, "code_fingerprint", lambda: "other")
    assert base != cache.key(kind="x", photos=["a", "b"])
    assert len(base) == 32 and cache.digest(b"data") == cache.digest(b"data")


def test_refreshing_is_scoped():
    assert cache.refresh_requested() is False
    with cache.refreshing():
        assert cache.refresh_requested() is True
        with cache.refreshing(False):
            assert cache.refresh_requested() is False
    assert cache.refresh_requested() is False


def _save(value, *, replace=False, **extra):
    return cache.save(
        cache.draft(
            cache.PRODUCT_CACHE,
            "k1",
            "analysis",
            value,
            inputs=[A],
            replace=replace,
        ),
        **extra,
    )


def _load():
    return cache.load(cache.PRODUCT_CACHE, "k1", "analysis")


TYPES = (Part,)


def test_an_entry_is_written_blobs_first_and_read_back(storage):
    crop = png(color=(9, 9, 9))
    entry = _save(_whole(crop), extra_field="x")
    written = list(storage.uploads)
    assert written == [
        storage.media(f"product_cache/k1/{entry}-b0.png"),  # blobs first
        storage.media("product_cache/k1/analysis.json"),  # the index last
    ]
    lookup = _load()
    assert lookup.index["entry"] == entry and lookup.index["extra_field"] == "x"
    assert lookup.decode(Whole, [A], TYPES) == _whole(crop)


def test_index_writes_are_create_only_unless_replacing(storage):
    first = _save(Whole([Part(A)], note="first"))
    assert _save(Whole([Part(A)], note="second")) is None
    assert _load().decode(Whole, [A], TYPES).note == "first"
    third = _save(Whole([Part(A)], note="third"), replace=True)
    assert third not in (None, first)
    assert _load().decode(Whole, [A], TYPES).note == "third"


def test_a_missing_index_is_a_plain_miss(storage):
    lookup = _load()
    assert (lookup.index, lookup.stale) == (None, False)
    assert lookup.decode(Whole) is None


@pytest.mark.parametrize(
    "damage",
    ["blob", "json", "version", "key", "value", "type"],
)
def test_a_broken_entry_is_a_stale_miss(storage, damage):
    _save(_whole(png(color=(9, 9, 9))))
    uri = storage.media("product_cache/k1/analysis.json")
    index = json.loads(storage.objects[uri])
    if damage == "blob":
        storage.delete(
            storage.media(f"product_cache/k1/{index['blobs']['b0']}")
        )
    elif damage == "json":
        storage.objects[uri] = b"{not json"
    elif damage in ("version", "key"):
        storage.objects[uri] = json.dumps({**index, damage: 0}).encode()
    elif damage == "value":
        index["value"]["$type"] = "Gone"
        storage.objects[uri] = json.dumps(index).encode()
    lookup = _load()
    expect = Part if damage == "type" else Whole
    assert lookup.decode(expect, [A], TYPES) is None
    assert lookup.stale is True


def test_storage_out_of_reach_is_a_miss(storage, monkeypatch):
    def unreachable(uri):
        raise OSError("no network")

    monkeypatch.setattr(gcs, "download", unreachable)
    assert _load().index is None


def test_a_failed_write_returns_none_and_logs(storage, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise OSError("bucket unavailable")

    monkeypatch.setattr(gcs, "create", broken)
    assert _save(Whole([Part(A)])) is None
    assert "could not store" in caplog.text


def test_gcs_create_writes_only_once(storage):
    uri = storage.media("product_cache/k/x.json")
    assert gcs.create(b"1", uri, content_type="application/json") is True
    assert gcs.create(b"2", uri, content_type="application/json") is False
    assert storage.objects[uri] == b"1"


def test_background_writes_do_not_block_and_failures_only_log(caplog):
    async def run():
        order = []
        cache.in_background(lambda: order.append("written"))

        def fail():
            raise OSError("down")

        cache.in_background(fail)
        order.append("went on")
        await asyncio.gather(*list(cache._WRITES), return_exceptions=True)
        await asyncio.sleep(0)
        return order

    assert asyncio.run(run()) == ["went on", "written"]
    assert "background write failed" in caplog.text
    assert not cache._WRITES
