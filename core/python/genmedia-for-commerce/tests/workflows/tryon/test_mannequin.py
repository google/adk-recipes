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

"""The standalone mannequin module: the paste-back on synthetic photos, the
edit call on a stub client."""

import ast
import io
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.workflows.tryon import mannequin

W, H = 240, 320
BG, SKIN, GREY = (245, 245, 245), (224, 172, 138), (175, 175, 175)
# The edit redraws each product slightly off (ΔE about 9 and 14).
SHIRT, SHIRT_EDIT = (190, 40, 50), (180, 52, 58)
BAG, BAG_EDIT = (40, 70, 160), (52, 80, 150)
PERSON = (slice(30, 300), slice(70, 170))  # head to legs
ON_BODY = (slice(110, 210), slice(60, 180))  # a shirt over the body
AWAY = (slice(200, 280), slice(200, 235))  # a bag 30 px from the person
HEAD = (slice(45, 95), slice(85, 155))  # inside the head


def _scene(person, shirt, bag) -> np.ndarray:
    a = np.full((H, W, 3), BG, np.uint8)
    a[PERSON], a[ON_BODY], a[AWAY] = person, shirt, bag
    return a


def _png(a: np.ndarray | Image.Image) -> bytes:
    image = a if isinstance(a, Image.Image) else Image.fromarray(a)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _rgb(data: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def _mask(region) -> np.ndarray:
    m = np.zeros((H, W), np.uint8)
    m[region] = 1
    return m


ORIGINAL = _scene(SKIN, SHIRT, BAG)
EDIT = _scene(GREY, SHIRT_EDIT, BAG_EDIT)  # the person became grey


def test_products_stay_original_and_the_person_takes_the_edit():
    out = _rgb(mannequin.paste_back(_png(ORIGINAL), _png(EDIT)))
    same = (out == ORIGINAL).all(axis=2)
    # The shirt over the body: bit-identical but for its 4 px edge (the
    # feathered mask's last trace); the bag away from the person: all.
    inside = cv2.erode(_mask(ON_BODY), np.ones((9, 9), np.uint8)) > 0
    assert same[inside].all()
    assert same[AWAY].all()
    # The person's skin is the edit's grey.
    assert np.abs(out[HEAD].astype(int) - EDIT[HEAD]).max() <= 1
    # The background away from the person is untouched.
    near = cv2.dilate(_mask(PERSON), np.ones((61, 61), np.uint8)) > 0
    assert same[~near].all()


def test_an_edit_with_no_person_gives_the_original_back():
    assert np.array_equal(
        _rgb(mannequin.paste_back(_png(ORIGINAL), _png(ORIGINAL))), ORIGINAL
    )


def test_the_result_is_a_png_at_the_originals_size():
    small = Image.fromarray(EDIT).resize((180, 240), Image.Resampling.LANCZOS)
    out = Image.open(
        io.BytesIO(mannequin.paste_back(_png(ORIGINAL), _png(small)))
    )
    assert (out.format, out.size) == ("PNG", (W, H))
    assert np.abs(np.asarray(out)[HEAD].astype(int) - EDIT[HEAD]).max() <= 2


class _Models:
    """``client.models`` answering ``generate_content`` with ``answer``."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.answer


def _response(*parts) -> SimpleNamespace:
    content = SimpleNamespace(parts=list(parts))
    candidate = SimpleNamespace(content=content, finish_reason="STOP")
    return SimpleNamespace(candidates=[candidate])


def _image_part(data: bytes) -> SimpleNamespace:
    return SimpleNamespace(inline_data=SimpleNamespace(data=data))


def test_normalize_edits_with_the_prompt_and_pastes_back():
    models = _Models(_response(_image_part(_png(EDIT))))
    client = SimpleNamespace(models=models)
    out = mannequin.normalize(_png(ORIGINAL), client=client, model="m")
    assert out == mannequin.paste_back(_png(ORIGINAL), _png(EDIT))
    (call,) = models.calls
    prompt, part = call["contents"]
    assert call["model"] == "m"
    assert prompt == mannequin.PROMPT
    assert (part.inline_data.mime_type, part.inline_data.data) == (
        "image/png",
        _png(ORIGINAL),
    )
    config = call["config"]
    assert config.response_modalities == ["IMAGE"]
    assert config.image_config.aspect_ratio is None  # the input's kept
    assert config.image_config.image_size == "2K"
    assert config.image_config.output_mime_type == "image/png"
    assert config.thinking_config.thinking_level == "MEDIUM"
    assert config.media_resolution == "MEDIA_RESOLUTION_HIGH"
    assert config.temperature == pytest.approx(0.1)


def test_normalize_raises_when_the_model_returns_no_image():
    text = SimpleNamespace(inline_data=None)
    client = SimpleNamespace(models=_Models(_response(text)))
    with pytest.raises(RuntimeError, match="no image"):
        mannequin.normalize(_png(ORIGINAL), client=client, model="m")


def test_the_module_needs_nothing_from_the_package():
    tree = ast.parse(Path(mannequin.__file__).read_text())
    imported = {
        name.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    third_party = {"cv2", "numpy", "google", "PIL"}
    assert "genmedia4commerce" not in imported
    assert imported - third_party <= {
        "argparse",
        "io",
        "logging",
        "math",
        "os",
        "pathlib",
    }
