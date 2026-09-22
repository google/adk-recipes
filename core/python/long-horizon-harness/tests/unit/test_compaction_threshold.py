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

"""Deterministic tests for per-model input limits + compaction threshold.

Never asserts on LLM output — only on the limit lookup and the integer
threshold computed from it.
"""

from __future__ import annotations

import pytest

from horizon.context.compaction_threshold import (
    DEFAULT_WINDOW_FRACTION,
    compaction_token_threshold,
)
from horizon.models.registry import (
    DEFAULT_INPUT_TOKEN_LIMIT,
    input_token_limit,
)
from horizon.models.selector import apply_compaction_threshold


class TestInputTokenLimit:
    def test_known_default_model(self) -> None:
        assert input_token_limit("gemini-3.7-flash") == 1_000_000

    def test_known_pro_model(self) -> None:
        assert input_token_limit("gemini-3.1-pro-preview") == 1_000_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        assert input_token_limit("does-not-exist") == DEFAULT_INPUT_TOKEN_LIMIT

    def test_none_falls_back_to_default(self) -> None:
        assert input_token_limit(None) == DEFAULT_INPUT_TOKEN_LIMIT


class TestCompactionTokenThreshold:
    def test_default_fraction_of_window(self) -> None:
        # 75% of a 200k window.
        assert compaction_token_threshold(200_000) == 150_000

    def test_scales_with_larger_window(self) -> None:
        assert compaction_token_threshold(1_000_000) == 750_000

    def test_env_override_fraction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LHA_COMPACTION_WINDOW_FRACTION", "0.5")
        assert compaction_token_threshold(200_000) == 100_000

    def test_invalid_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LHA_COMPACTION_WINDOW_FRACTION", "not-a-number")
        expected = int(200_000 * DEFAULT_WINDOW_FRACTION)
        assert compaction_token_threshold(200_000) == expected

    def test_out_of_range_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # >1.0 would leave no headroom; ignore it.
        monkeypatch.setenv("LHA_COMPACTION_WINDOW_FRACTION", "1.5")
        expected = int(200_000 * DEFAULT_WINDOW_FRACTION)
        assert compaction_token_threshold(200_000) == expected

    def test_never_returns_non_positive(self) -> None:
        # Pathologically tiny window still yields a positive threshold.
        assert compaction_token_threshold(1) >= 1


class _Cfg:
    def __init__(self) -> None:
        self.token_threshold = 750_000
        self.event_retention_size = 20


class TestApplyCompactionThreshold:
    def test_sets_threshold_for_gemini(self) -> None:
        cfg = _Cfg()
        apply_compaction_threshold(cfg, "gemini-3.7-flash")
        assert cfg.token_threshold == 750_000  # 75% of 1M

    def test_unknown_model_uses_default_limit(self) -> None:
        cfg = _Cfg()
        apply_compaction_threshold(cfg, "mystery")
        assert cfg.token_threshold == 150_000  # default 200k window

    def test_none_config_is_noop(self) -> None:
        # Must not raise when no compaction config is present (e.g. tests).
        apply_compaction_threshold(None, "gemini-3.7-flash")

    def test_leaves_retention_size_untouched(self) -> None:
        cfg = _Cfg()
        apply_compaction_threshold(cfg, "gemini-3.7-flash")
        assert cfg.event_retention_size == 20
