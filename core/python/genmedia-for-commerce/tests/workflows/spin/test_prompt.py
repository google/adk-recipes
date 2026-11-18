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

import hashlib

import pytest

from genmedia4commerce.workflows.spin.prompt import (
    SHOE_SPIN_PROMPT,
    TURN_DURATION,
    orbit_turns,
    shoe_prompt,
)

# sha256 of the tuned omni shoe prompt (last tuned: back at the start by about
# 8.5 s and keep turning): the text must stay byte-identical.
_SHOE_PROMPT_SHA256 = (
    "cc5fb2da07f6cb79879bb2f11ad3962af4ea0ca256881753125d3335f992023a"
)


def test_shoe_prompt_is_the_tuned_prompt_verbatim():
    digest = hashlib.sha256(SHOE_SPIN_PROMPT.encode()).hexdigest()
    assert digest == _SHOE_PROMPT_SHA256


@pytest.mark.parametrize("aspect_ratio", ["16:9", "9:16"])
def test_shoe_prompt_fills_the_aspect_ratio(aspect_ratio):
    prompt = shoe_prompt(aspect_ratio)
    assert f"{aspect_ratio} aspect ratio" in prompt
    assert "{" not in prompt


def test_without_mirrored_references_the_shoe_prompt_is_the_tuned_one():
    tuned = SHOE_SPIN_PROMPT.replace("{aspect_ratio}", "16:9")
    assert shoe_prompt("16:9") == shoe_prompt("16:9", []) == tuned


@pytest.mark.parametrize(
    ("positions", "sentence"),
    [
        (
            [2],
            "Reference image 2 is a generated mirror image of the other shoe "
            "of the pair: use it only for the shoe's shape, colour and "
            "materials; take text and logos only from the other references.",
        ),
        (
            [3, 5],
            "Reference images 3 and 5 are generated mirror images of the "
            "other shoe of the pair: use them only for the shoe's shape, "
            "colour and materials; take text and logos only from the other "
            "references.",
        ),
        ([1, 4, 6], "Reference images 1, 4 and 6 are generated mirror images"),
    ],
)
def test_mirrored_references_add_one_sentence(positions, sentence):
    prompt = shoe_prompt("9:16", positions)
    tuned = shoe_prompt("9:16")
    assert prompt.startswith(tuned + "\n\nReference image")
    assert sentence in prompt[len(tuned) :]
    assert prompt.count("generated mirror image") == 1


def test_the_first_half_turns_from_the_front_to_the_back():
    first, _ = orbit_turns("a black SUV", ["front", "right", "back", "left"])
    assert first.startswith(f"[duration={TURN_DURATION}]")
    assert "halfway around a stationary product (180 degrees total)" in first
    assert "The product is a black SUV" in first
    assert "<IMAGE_0>, <IMAGE_1>, <IMAGE_2>, <IMAGE_3>" in first
    assert "front of the centred product matching <IMAGE_0>" in first
    assert "the back view matching <IMAGE_2>" in first
    # Timestamps open intervals: the destination comes before the last second.
    assert "[00:03.000]" in first and "[00:04.000]" not in first


def test_the_second_half_continues_back_to_the_front():
    _, second = orbit_turns("a red tote", ["front", "back"])
    assert second.startswith(f"[duration={TURN_DURATION}] Continue")
    assert "same direction for the remaining 180 degrees" in second
    assert "front view matching <IMAGE_0>" in second
    assert "[00:02.500]" in second and "[00:04.000]" not in second


def test_without_a_back_photo_the_back_is_not_tied_to_an_image():
    first, _ = orbit_turns("a drop earring", ["front"])
    assert "the back view and keeps" in first
    assert "<IMAGE_1>" not in first


def test_both_halves_are_silent_and_keep_the_product_rules():
    first, second = orbit_turns("a bag", ["front", "back"])
    for prompt in (first, second):
        lines = [
            line for line in prompt.splitlines() if line.startswith("[00:")
        ]
        assert lines and all(
            line.endswith("Audio: No sound, completely muted audio track.")
            for line in lines
        )
    assert "no turntable, floor or shadow" in first
    assert "removable accessory" in first and "left off" in first
