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

import pytest

from genmedia4commerce.core.errors import (
    DeadlineExceeded,
    GenerationError,
    InputError,
)
from genmedia4commerce.workflows.spin import classify as classify_mod
from genmedia4commerce.workflows.spin.classify import classify

UNRANKED = classify_mod.UNRANKED


def _response(product_type="shoes", description="a white leather sneaker", n=3):
    views = ["right", "front", "back"]
    return {
        "product_type": product_type,
        "description": description,
        "classifications": [
            {
                "index": i,
                "view": views[i % 3],
                "rank": 1,
                "presentation": "worn" if i == 1 else "flat",
            }
            for i in range(n)
        ],
    }


def test_shoes(fake_gen, images):
    fake_gen.json(_response("shoes"))
    result = classify(images)
    assert result.product_type == "shoes"
    assert result.description == "a white leather sneaker"
    assert result.views == [
        {"index": 0, "view": "right", "rank": 1, "presentation": "flat"},
        {"index": 1, "view": "front", "rank": 1, "presentation": "worn"},
        {"index": 2, "view": "back", "rank": 1, "presentation": "flat"},
    ]


def test_other(fake_gen, images):
    fake_gen.json(_response("other", "a black smartphone"))
    result = classify(images)
    assert result.product_type == "other"
    assert result.description == "a black smartphone"


@pytest.mark.parametrize("product_type", ["cars", "glasses", None])
def test_unknown_product_type_maps_to_other(fake_gen, images, product_type):
    fake_gen.json(_response(product_type))
    assert classify(images).product_type == "other"


def test_one_call_with_labelled_images(fake_gen, images):
    fake_gen.json(_response())
    classify(images)
    assert len(fake_gen.calls) == 1
    call = fake_gen.calls[0]
    assert call["fn"] == "generate_json"
    parts = call["raw_parts"]
    for i, image in enumerate(images):
        assert parts[parts.index(f"IMAGE_{i}:") + 1] == image
    assert "3 image(s)" in call["system"]
    assert call["schema"]["required"] == [
        "product_type",
        "description",
        "classifications",
    ]


def test_the_judge_model_labels_views_by_where_the_front_points(
    fake_gen, images
):
    from genmedia4commerce.core.models import get_models

    fake_gen.json(_response())
    classify(images)
    call = fake_gen.calls[0]
    assert call["params"].model == get_models().judge
    assert call["params"].thinking == "MEDIUM"
    # picture-based, as for shoes: no "the product's own right"
    assert "Where the product's FRONT points in the picture" in call["system"]
    assert "own right" not in call["system"]


def test_missing_and_invalid_entries_get_defaults(fake_gen, images):
    fake_gen.json(
        {
            "product_type": "other",
            "description": "a mug",
            "classifications": [
                {
                    "index": 2,
                    "view": "top",
                    "rank": "high",
                    "presentation": "?",
                },
                {"index": 0, "view": "left", "rank": 0},
            ],
        }
    )
    assert classify(images).views == [
        {"index": 0, "view": "left", "rank": UNRANKED, "presentation": "flat"},
        {"index": 1, "view": "other", "rank": UNRANKED, "presentation": "flat"},
        {"index": 2, "view": "other", "rank": UNRANKED, "presentation": "flat"},
    ]


def test_missing_description_has_fallback(fake_gen, images):
    fake_gen.json(_response(description=""))
    assert classify(images).description == classify_mod.DEFAULT_DESCRIPTION


def test_generation_error_propagates(fake_gen, images):
    fake_gen.json(GenerationError("model down", retryable=True))
    with pytest.raises(GenerationError):
        classify(images)


def test_no_images_is_input_error(fake_gen):
    with pytest.raises(InputError):
        classify([])


def test_three_quarter_views_are_kept_as_their_own_angle(fake_gen, images):
    fake_gen.json(
        {
            "product_type": "other",
            "description": "a black SUV",
            "classifications": [
                {"index": 0, "view": "front_right", "rank": 1},
                {"index": 1, "view": "back_left", "rank": 1},
                {"index": 2, "view": "side", "rank": 1},
            ],
        }
    )
    views = [v["view"] for v in classify(images).views]
    assert views == ["front_right", "back_left", "other"]


def test_a_stuck_call_fails_after_the_shared_retry(fake_gen, images):
    # core.gen asks a stalled call again once; classify adds no loop.
    fake_gen.json(DeadlineExceeded("no answer within 40s"))
    with pytest.raises(DeadlineExceeded):
        classify(images)
    calls = [c for c in fake_gen.calls if c["fn"] == "generate_json"]
    assert len(calls) == 1
    assert calls[0]["params"].deadline_s == classify_mod.CLASSIFY_TIMEOUT_S
