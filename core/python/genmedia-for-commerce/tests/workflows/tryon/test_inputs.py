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

"""The wearer photo as the try-on's models get it (``inputs.wearer``)."""

import asyncio

import pytest

from genmedia4commerce.core import media
from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows.tryon import inputs

PHOTO = png(120, 160, color=(230, 200, 180))
BOX = (40.0, 20.0, 80.0, 70.0)  # Cloud Vision's face box in the photo


@pytest.fixture
def vision(monkeypatch) -> list[bytes]:
    """Cloud Vision faked: ``BOX`` for any photo; each photo sent kept."""
    sent: list[bytes] = []

    def face_box(data: bytes):
        sent.append(data)
        return BOX

    monkeypatch.setattr(media, "face_box", face_box)
    return sent


def test_the_body_is_the_photo_and_both_faces_its_crop(vision):
    body, eval_face, prompt_face = asyncio.run(inputs.wearer(PHOTO))
    assert body is PHOTO  # as it is: nothing removed, nothing cropped
    assert vision == [PHOTO]
    crop = media.crop_box(PHOTO, BOX, pad=media.FACE_MARGIN)
    assert eval_face == crop and prompt_face == crop


def test_no_face_gives_the_photo_alone(monkeypatch):
    monkeypatch.setattr(media, "face_box", lambda data: None)
    body, eval_face, prompt_face = asyncio.run(inputs.wearer(PHOTO))
    assert body is PHOTO
    assert (eval_face, prompt_face) == (None, None)


def test_upscale_face_changes_only_the_prompts_face(vision, monkeypatch):
    sharper = png(32, 32, color=(1, 2, 3))
    monkeypatch.setattr(inputs, "_sharper_face", lambda face: sharper)
    body, eval_face, prompt_face = asyncio.run(
        inputs.wearer(PHOTO, upscale_face=True)
    )
    assert body is PHOTO
    assert eval_face == media.crop_box(PHOTO, BOX, pad=media.FACE_MARGIN)
    assert prompt_face == sharper
