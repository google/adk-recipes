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

import os

import numpy as np
import pytest

from genmedia4commerce.core import clients, gen, video
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.gen import ImageParams, TextParams, VideoParams
from genmedia4commerce.core.media import sniff_mime
from genmedia4commerce.core.testing import mp4_stub, png


def test_png_and_mp4_helpers():
    assert sniff_mime(png()) == "image/png"
    clip = mp4_stub(frames=6)
    assert sniff_mime(clip) == "video/mp4"
    assert video.probe(clip)[0] == 6


def test_fake_text_and_calls(fake_gen):
    fake_gen.text("hello")
    params = TextParams(thinking="HIGH")
    assert (
        gen.generate_text(["q", png(4, 4)], system="sys", params=params)
        == "hello"
    )
    call = fake_gen.calls[-1]
    assert call["fn"] == "generate_text"
    assert call["parts"][0] == "q"
    assert call["parts"][1].startswith("<image/png ")
    assert call["raw_parts"][1] == png(4, 4)
    assert call["system"] == "sys"
    assert call["params"] is params


def test_fake_records_default_params(fake_gen):
    fake_gen.image(b"IMG")
    assert gen.generate_image(["x"]) == b"IMG"
    assert fake_gen.calls[-1]["params"] == ImageParams()
    assert fake_gen.calls[-1]["system"] is None


def test_fake_json_queue_and_last_repeats(fake_gen):
    fake_gen.json([{"n": 1}, {"n": 2}])
    assert [gen.generate_json(["q"]) for _ in range(3)] == [
        {"n": 1},
        {"n": 2},
        {"n": 2},
    ]


def test_fake_json_single_and_list_response(fake_gen):
    fake_gen.json({"a": 1})
    assert gen.generate_json(["q"], schema={"type": "OBJECT"}) == {"a": 1}
    assert fake_gen.calls[-1]["schema"] == {"type": "OBJECT"}
    fake_gen.json([[1, 2]])  # a top-level JSON list response, wrapped
    assert gen.generate_json(["q"]) == [1, 2]


def test_fake_json_returns_copies(fake_gen):
    response = {"items": [1]}
    fake_gen.json(response)
    gen.generate_json(["q"])["items"].append(2)
    assert gen.generate_json(["q"]) == {"items": [1]}


def test_fake_callable_and_exception(fake_gen):
    fake_gen.image(lambda parts, **kw: b"for-" + parts[0].encode())
    assert gen.generate_image(["cat"]) == b"for-cat"
    fake_gen.image([GenerationError("boom"), b"ok"])
    with pytest.raises(GenerationError):
        gen.generate_image(["x"])
    assert gen.generate_image(["x"]) == b"ok"


def test_fake_video_and_embed(fake_gen):
    fake_gen.video(b"MP4")
    params = VideoParams(aspect_ratio="9:16")
    assert gen.generate_video([png()], "spin", params=params) == b"MP4"
    call = fake_gen.calls[-1]
    assert call["fn"] == "generate_video"
    assert call["prompt"] == "spin" and call["params"] is params
    assert len(call["raw_parts"]) == 1
    fake_gen.embed(np.ones((2, 3), dtype=np.float32))
    out = gen.embed(["a", "b"], model="emb", output_dims=3)
    assert out.shape == (2, 3)
    assert fake_gen.calls[-1]["model"] == "emb"
    assert fake_gen.calls[-1]["location"] is None


def test_fake_unset_kind_raises(fake_gen):
    with pytest.raises(AssertionError, match="no video responder"):
        gen.generate_video([png()], "p")


def test_fake_rejects_wrong_signature(fake_gen):
    fake_gen.image(b"x")
    with pytest.raises(TypeError):
        gen.generate_image(["x"], quality="high")


def test_conftest_hermetic_defaults(tmp_path):
    assert os.environ["PROJECT_ID"] == "test-project"
    assert os.environ["GENMEDIA_CATALOG_PATH"] == str(
        tmp_path / "catalog" / "products.parquet"
    )
    with pytest.raises(AssertionError, match="real client in unit test"):
        clients.get_client(location="us-central1")
