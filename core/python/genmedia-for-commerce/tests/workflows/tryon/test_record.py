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

"""The try-on's run record: built from a run, read back for a video."""

import json

import pytest

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.testing import png
from genmedia4commerce.core.types import Classification, Product, ProductRef
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import outfit, record

FRONT, BACK = png(10, 10, (1, 0, 0)), png(11, 10, (2, 0, 0))
BAND, SHOES = png(12, 10, (3, 0, 0)), png(13, 10, (4, 0, 0))
ZOOM = png(6, 6, (5, 0, 0))
CROPS = {"top": BAND, "item-0": SHOES}
DETAIL = Detail(
    "logo", "MO", "front", "on the chest", ZOOM, 0, (1, 2, 3, 4), "monogram"
)


def _classification() -> Classification:
    top = Product(
        "A",
        "top",
        "a shirt",
        [
            ProductRef(FRONT, "front"),
            ProductRef(BACK, "back", presentation="worn"),
            ProductRef(BAND, "front", presentation="worn", source="model_crop"),
        ],
    )
    shoes = Product(
        "B",
        "footwear",
        "boots",
        [ProductRef(SHOES, "front", presentation="worn", source="model_crop")],
    )
    return Classification("outfit", [top, shoes], "full_body")


def _kept(closeups=(("A", DETAIL),)):
    """A record as a later run reads it: JSON and new bytes objects."""
    c = _classification()
    chosen = outfit.choose_garment_images(c)
    classified = record.payload(c, chosen, [FRONT, BACK], CROPS)
    event = record.record(classified, chosen, [FRONT, BACK], CROPS, closeups)
    files = {name: bytes(bytearray(data)) for name, data in event.files.items()}
    return json.loads(json.dumps(event.payload)), files, event


def test_the_record_names_every_photo_once_and_the_chosen_ones():
    manifest, _files, event = _kept()
    assert set(event.files) == {
        "photo-0",
        "photo-1",
        "crop-top",
        "crop-item-0",
        "closeup-A-0",
    }
    assert manifest["classification"]["products"][1]["chosen"] == {
        "source": "model_crop",
        "presentation": "worn",
        "band": "item-0",
    }
    top, shoes = manifest["products"]
    assert [r["file"] for r in top["refs"]] == [
        "photo-0",
        "photo-1",
        "crop-top",
    ]
    assert top["chosen"] == 0 and shoes["chosen"] == 0
    assert top["closeups"][0]["file"] == "closeup-A-0"
    assert shoes["closeups"] == []


def test_a_record_reads_back_as_the_videos_products():
    manifest, files, _ = _kept()
    source = record.read(manifest, files)
    assert source.photos == [FRONT, SHOES]  # the photo chosen per product
    c = source.classification
    assert (c.mode, c.framing) == ("outfit", "full_body")
    assert [(p.id, p.category) for p in c.products] == [
        ("A", "top"),
        ("B", "footwear"),
    ]
    assert c.products[0].refs == _classification().products[0].refs
    assert source.closeups == [[DETAIL], []]


def test_a_try_on_without_the_close_up_search_records_none():
    manifest, files, _ = _kept(closeups=None)
    assert record.read(manifest, files).closeups is None


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(version=2),
        lambda m: m["products"].pop(),
        lambda m: m["classification"].update(framing="sideways"),
        lambda m: m["products"][0]["refs"][0].update(file="missing"),
    ],
)
def test_anything_else_is_not_a_record(change):
    manifest, files, _ = _kept()
    change(manifest)
    with pytest.raises(InputError, match="not a try-on run record"):
        record.read(manifest, files)


def test_the_video_gets_each_products_best_upload_not_its_hero_crop():
    """A try-on drew the shirt from its look crop (its hero): the video
    gets the sharp upload, with the close-ups found in it; the shoes the
    look added have only their crop."""
    c = _classification()
    top = c.products[0]
    top.refs = [top.refs[2], *top.refs[:2]]  # the crop is the hero
    chosen = outfit.choose_garment_images(c)
    classified = record.payload(c, chosen, [FRONT, BACK], CROPS)
    event = record.record(classified, chosen, [FRONT, BACK], CROPS, [])
    manifest = json.loads(json.dumps(event.payload))
    assert manifest["products"][0]["chosen"] == 0  # the drawing's: the crop
    files = {name: bytes(bytearray(data)) for name, data in event.files.items()}
    assert record.read(manifest, files).photos == [FRONT, SHOES]
