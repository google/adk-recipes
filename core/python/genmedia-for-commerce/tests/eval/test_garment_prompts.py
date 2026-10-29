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

from typing import get_args

import pytest

from genmedia4commerce.core.types import Category
from genmedia4commerce.eval import garment_prompts as gp


@pytest.mark.parametrize(
    ("prompt", "anchor"),
    [
        (gp.DETECTOR_SYSTEM, "You are an expert garment QA evaluator."),
        (gp.VERIFIER_SYSTEM, "You double-check ONE QA flag"),
        (gp.AGGREGATE_SYSTEM, "CRITICAL CROSS-INPUT RULE"),
    ],
)
def test_prompt_files_loaded_with_anchor(prompt, anchor):
    assert prompt == prompt.strip()
    assert anchor in prompt


def test_aggregate_prompt_has_no_template_placeholder():
    assert "{STYLE_INSTRUCTION}" not in gp.AGGREGATE_SYSTEM


def test_product_filter_hints_cover_every_category():
    assert set(gp.PRODUCT_FILTER_HINTS) == set(get_args(Category))
    assert all(hint.strip() for hint in gp.PRODUCT_FILTER_HINTS.values())


def test_detector_focus_formats_product():
    text = gp.DETECTOR_FOCUS.format(product="blue jeans")
    assert 'only evaluate features of the "blue jeans" garment' in text
    assert "{" not in text


@pytest.mark.parametrize(
    "prompt", [gp.DETECTOR_LAYERING, gp.AGGREGATE_LAYERING]
)
def test_layering_notes_state_a_fact(prompt):
    text = prompt.format(visibility="sleeves visible from the armholes")
    assert "LAYERING, by design" in text
    assert '"sleeves visible from the armholes"' in text
    assert "HINT" not in text and "{" not in text


def test_schemas_require_verdict_fields():
    assert gp.VERDICT_SCHEMA["required"] == [
        "publish",
        "imperfection_count",
        "description",
    ]
    assert gp.DETECTOR_SCHEMA["required"] == [
        "product_present",
        "silhouette",
        "fabric_color",
        "features",
    ]
