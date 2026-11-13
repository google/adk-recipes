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

"""App events of the Studio as history sentences."""

from __future__ import annotations

from genmedia4commerce.studio.events import (
    finished_note,
    narrate,
    started_note,
)

ITEM = {"label": "jacket.jpg", "ref": "gs://m/runs/studio_uploads/j.png"}


def test_upload_carries_description_and_ref() -> None:
    text = narrate(
        {
            "type": "upload",
            "field": "product",
            "item": ITEM,
            "description": "a black leather jacket",
        }
    )
    assert text == (
        "User uploaded jacket.jpg in Products: a black leather jacket "
        "ref: gs://m/runs/studio_uploads/j.png"
    )


def test_select_names_the_source_and_field() -> None:
    text = narrate(
        {
            "type": "select",
            "source": "gallery",
            "field": "person",
            "item": {
                "label": "Emma",
                "ref": "gs://m/runs/studio_gallery/e.jpg",
            },
            "description": "woman, long dark hair",
        }
    )
    assert text == (
        "User picked Emma (woman, long dark hair) from the gallery as Person. "
        "ref: gs://m/runs/studio_gallery/e.jpg"
    )
    catalog = narrate(
        {
            "type": "select",
            "source": "catalog",
            "field": "product",
            "item": ITEM,
        }
    )
    assert catalog is not None
    assert "from the catalogue as Products" in catalog


def test_other_events() -> None:
    assert narrate({"type": "remove", "field": "product", "item": ITEM}) == (
        "User removed jacket.jpg from Products. ref: gs://m/runs/studio_uploads/j.png"
    )
    assert (
        narrate({"type": "clear", "field": "spin"})
        == "User cleared Spin photos."
    )
    assert narrate({"type": "page", "capability": "video_tryon"}) == (
        "User switched to the Video Try-On page."
    )
    assert narrate(
        {"type": "settings", "key": "scenario", "value": "beach"}
    ) == ("User set scenario to 'beach'.")
    assert narrate({"type": "animate", "item": ITEM}) is None  # retired
    search = narrate(
        {
            "type": "search",
            "query": "black tee",
            "items": [ITEM, ITEM, ITEM, ITEM],
        }
    )
    assert search is not None
    assert search.count("gs://m/runs/studio_uploads/j.png") == 3
    empty = narrate({"type": "search", "query": "x"})
    assert empty is not None
    assert empty.endswith("no results.")


def test_unknown_events_are_not_recorded() -> None:
    assert narrate({"type": "sync"}) is None
    assert narrate({}) is None


def test_generation_notes_carry_inputs_and_outputs() -> None:
    spec = {
        "capability": "spin",
        "spin": ["gs://a", "gs://b"],
        "settings": {"resolution": "720p", "prompt": ""},
    }
    assert started_note(spec) == (
        "Started 360° Spin; spin: [gs://a, gs://b]; "
        "settings: {'resolution': '720p'}."
    )
    best = {"uri": "gs://v1", "score": 91.6, "view": "front", "note": "ok"}
    assert finished_note("spin", [best, {"uri": "gs://v2"}]) == (
        "360° Spin finished. Best: gs://v1 (front, score 92, ok); "
        "others: gs://v2 ()."
    )
