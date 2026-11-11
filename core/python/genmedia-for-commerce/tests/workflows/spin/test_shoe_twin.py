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

import numpy as np
from PIL import Image

from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.workflows.spin import shoe_twin
from genmedia4commerce.workflows.spin.shoe_prompts import twin_prompt


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _shoe() -> np.ndarray:
    """A white photo of a grey shoe with its collar on the left."""
    arr = np.full((200, 400, 3), 255, np.uint8)
    arr[100:160, 80:320] = 120
    arr[50:100, 80:160] = 120
    return arr


SHOE = _png(_shoe())
MIRRORED = _png(_shoe()[:, ::-1].copy())


def test_the_flip_check():
    assert shoe_twin.flipped(MIRRORED, SHOE)
    assert not shoe_twin.flipped(SHOE, SHOE)


def test_the_first_flipped_output_is_kept(fake_gen):
    fake_gen.image([SHOE, MIRRORED])  # unchanged, then flipped
    out = shoe_twin.twin(SHOE, "front_right", "front_left")
    assert (np.asarray(Image.open(io.BytesIO(out))) == _shoe()[:, ::-1]).all()
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_image"]
    assert [c["raw_parts"] for c in calls] == [
        [twin_prompt("front_right", "front_left"), SHOE]
    ] * 2
    assert calls[0]["params"].image_size == "2K"
    assert calls[0]["params"].thinking == "MEDIUM"


def test_no_twin_after_the_last_try(fake_gen):
    fake_gen.image([GenerationError("no image"), SHOE])
    assert shoe_twin.twin(SHOE, "right", "left") is None
    assert len(fake_gen.calls) == shoe_twin.TRIES


def test_a_refused_photo_is_not_asked_again(fake_gen):
    fake_gen.image(GenerationError("finish_reason=IMAGE_PROHIBITED_CONTENT"))
    assert shoe_twin.twin(SHOE, "right", "left") is None
    assert len(fake_gen.calls) == 1


def test_twins_keep_the_order_of_the_jobs(fake_gen):
    # the first job's photo comes back mirrored, the second's unchanged
    fake_gen.image(
        lambda parts, **kw: MIRRORED if parts[1] == SHOE else parts[1]
    )
    out = shoe_twin.twins(
        [(SHOE, "right", "left"), (MIRRORED, "left", "right")]
    )
    assert out[0] is not None and out[1] is None
