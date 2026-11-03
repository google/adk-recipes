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

"""The local catalogue (core.catalog) and its search (core.catalog_search)."""

import io
import threading

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import catalog, catalog_search
from genmedia4commerce.core.errors import (
    ConfigError,
    GenerationError,
    InputError,
)


def _axis(i: int) -> np.ndarray:
    vector = np.zeros((1, 256), dtype=np.float32)
    vector[0, i] = 3.0  # not unit: the search normalises it
    return vector


def test_a_missing_file_is_a_config_error():
    with pytest.raises(ConfigError, match="missing"):
        catalog.get_catalog()


def test_loaded_once_even_from_many_threads(catalog_file, monkeypatch):
    loads = []
    real = catalog._load
    monkeypatch.setattr(catalog, "_load", lambda p: loads.append(p) or real(p))
    threads = [threading.Thread(target=catalog.get_catalog) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == [catalog_file]


def test_lists_and_lookups(catalog_file):
    assert [r["id"] for r in catalog.featured()] == ["pa1", "p_glasses_4"]
    assert [r["id"] for r in catalog.featured("men")] == ["p_glasses_4"]
    assert [r["id"] for r in catalog.featured(family="tops")] == ["pa1"]
    assert [r["id"] for r in catalog.models()] == ["m_emma"]
    assert [(r["id"], r["images"]) for r in catalog.spin_sets()] == [
        ("s_shoe", 3)
    ]
    assert catalog.item("pb2")["title"] == "Black leather jacket"
    with pytest.raises(InputError):
        catalog.item("nope")


def test_refs(catalog_file):
    assert catalog.ref("pa1") == "catalog:pa1"
    assert catalog.ref("s_shoe", 2) == "catalog:s_shoe/2"
    assert catalog.parse_ref("catalog:pa1") == ("pa1", 0)
    assert catalog.parse_ref("catalog:s_shoe/2") == ("s_shoe", 2)
    assert catalog.is_ref("catalog:x") and not catalog.is_ref("gs://b/x")
    for bad in (
        "catalog:s_shoe/3",
        "catalog:pa1/-1",
        "catalog:pa1/",
        "catalog:",
        "gs://b/x",
    ):
        with pytest.raises(InputError):
            catalog.parse_ref(bad)


def test_images_and_cached_thumbnails(catalog_file):
    full = catalog.image("s_shoe", 1)
    assert full[:4] == b"RIFF" and full != catalog.image("s_shoe", 0)
    thumb = catalog.thumbnail("pa1", 0, 100)
    with Image.open(io.BytesIO(thumb)) as img:
        assert (img.format, img.size) == ("WEBP", (75, 100))
    assert catalog.thumbnail("pa1", 0, 100) is thumb  # cached
    with pytest.raises(InputError):
        catalog.thumbnail("pa1", 1)


def test_source_ref_maps_images_of_the_old_catalogue(catalog_file):
    assert catalog.source_ref("gs://old-assets/b2.jpg") == "catalog:pb2"
    assert catalog.source_ref("gs://old-assets/zz.jpg") is None


def test_search_ranks_by_meaning_plus_keywords(catalog_file, fake_gen):
    fake_gen.embed(_axis(1))  # closest to the jacket
    found = catalog_search.search("something warm", k=3)
    assert [r["id"] for r in found] == ["pb2", "pa1", "pc3"]
    call = fake_gen.calls[0]
    assert call["output_dims"] == 256 and call["model"] == (
        "gemini-embedding-001"
    )
    # A keyword match lifts a product without an embedding into the results
    # (a full match ranks like a good semantic match: 0.8 here vs 0.58).
    fake_gen.embed(_axis(0) + _axis(1) + _axis(2))
    found = catalog_search.search("amber sunglasses", k=2)
    assert [r["id"] for r in found] == ["p_glasses_4", "pa1"]


def test_search_filters(catalog_file, fake_gen):
    fake_gen.embed(_axis(0))
    women = catalog_search.search("tee", audience="women", k=10)
    assert {r["id"] for r in women} == {"pa1", "pc3"}  # unisex too
    assert [r["id"] for r in catalog_search.search("x", family="bottoms")] == [
        "pc3"
    ]
    everything = catalog_search.search("tee", k=10)
    assert all(r["kind"] == "product" for r in everything)
    assert "p_glasses_4" not in {r["id"] for r in everything}  # no keyword


def test_keyword_only_when_the_embedding_fails(catalog_file, fake_gen):
    fake_gen.embed(GenerationError("down"))
    found = catalog_search.search("black jackets", k=5)
    assert [r["id"] for r in found] == ["pb2"]
    assert catalog_search.search("nothing like it", k=5) == []


@pytest.mark.parametrize("k", [0, 51])
def test_search_checks_its_arguments(catalog_file, k):
    with pytest.raises(InputError):
        catalog_search.search("tee", k=k)
    with pytest.raises(InputError):
        catalog_search.search("  ")


def test_find_lists_the_featured_products_for_an_empty_query(catalog_file):
    assert [r["id"] for r in catalog_search.find("", k=1)] == ["pa1"]
    assert [r["id"] for r in catalog_search.find(" ", audience="men")] == [
        "p_glasses_4"
    ]
