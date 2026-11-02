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

"""The Studio agent's settings tool: it edits the turn's working state
(``studio.turn.current().st``) and records a ``settings`` patch for the
app, keeping only the values each setting takes (``studio.patches``)."""

from __future__ import annotations

from typing import Any

from genmedia4commerce.core import gen
from genmedia4commerce.studio import patches as rules
from genmedia4commerce.studio import turn

__all__ = ["update_settings"]

_RATIOS = {
    "tryon": gen.IMAGE_ASPECT_RATIOS,
    "video_tryon": gen.VIDEO_ASPECT_RATIOS,
    "spin": ("auto", *gen.VIDEO_ASPECT_RATIOS),
}
_SWITCH = {"on": True, "off": False}


def _settings(cap: str) -> dict[str, Any]:
    return rules.settings_of(turn.current().st, cap)


def _options(complete_look: str, upscale_crops: str) -> dict[str, Any]:
    """The try-on options given as "on" or "off", set on the Photo Try-On
    page; the others are left unchanged."""
    values = {
        "complete_look": _SWITCH.get(complete_look),
        "upscale_crops": _SWITCH.get(upscale_crops),
    }
    changed = {k: v for k, v in values.items() if v is not None}
    _settings("tryon").update(changed)
    return changed


def update_settings(
    scenario: str = "",
    style_guidelines: str = "",
    aspect_ratio: str = "",
    motion: str = "",
    motion_prompt: str = "",
    num_variations: int = 0,
    complete_look: str = "",
    upscale_crops: str = "",
) -> dict[str, Any]:
    """Fill in settings. Empty arguments stay unchanged.

    Args:
        scenario: The scene, e.g. "a neon-lit street at night".
        style_guidelines: Styling rules, e.g. "jacket open, sleeves rolled".
        aspect_ratio: For the current page: try-on "3:4", "1:1", "4:3",
            "9:16", "16:9" and other image ratios; video try-on "9:16" or
            "16:9"; spin "auto" (from the product's shape), "16:9" or "9:16".
        motion: Video: "auto" (shots planned from the images: a walk,
            close-ups of products, a turn when there is a back view) or
            "custom" (needs motion_prompt).
        motion_prompt: What the video should show, in plain words.
        num_variations: 1, 2 or 3 variations (current page); 0 = unchanged.
        complete_look: "on" or "off": with a reference model, also dress the
            person in what only the reference model wears (on by default).
        upscale_crops: "on" or "off": sharpen the shoes cut from the
            reference model photo first, slower (off by default).
    """
    cap = str(turn.current().st.get("capability") or "tryon")
    changed: dict[str, Any] = {}
    for key, value in zip(
        ("scenario", "style_guidelines"),
        (scenario, style_guidelines),
        strict=True,
    ):
        if value:
            _settings("tryon")[key] = changed[key] = value
    if motion_prompt or motion == "auto":
        vto = _settings("video_tryon")
        vto["motion"] = changed["motion"] = (
            "custom" if motion_prompt else "auto"
        )
        vto["prompt"] = changed["prompt"] = motion_prompt
    if aspect_ratio in _RATIOS.get(cap, ()):
        _settings(cap)["aspect_ratio"] = changed["aspect_ratio"] = aspect_ratio
    # Only a new count is a change (``tool_order.COST_SETTINGS``: the
    # generation needs confirming again).
    count = str(num_variations)
    if num_variations in (1, 2, 3) and (
        str(_settings(cap).get("num_variations")) != count
    ):
        _settings(cap)["num_variations"] = changed["num_variations"] = count
    changed.update(_options(complete_look, upscale_crops))
    turn.patch("settings", changed)
    return {"settings": changed}
