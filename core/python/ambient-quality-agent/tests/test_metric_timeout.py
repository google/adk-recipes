# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for bounding how long one metric may take on one session."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from ambient_quality_agent.tools.metrics.library import CodeMetric
from ambient_quality_agent.tools.metrics.timeout import wrap_with_timeout


def test_a_metric_that_never_returns_is_abandoned() -> None:
    """Without this the node waits forever, the run never finishes, and the
    window never advances -- so every later sweep waits behind it too."""
    release = threading.Event()

    def hangs(instance: Any) -> Any:
        release.wait(timeout=30)
        return 1.0

    try:
        with pytest.raises(TimeoutError, match="did not return"):
            wrap_with_timeout(hangs, seconds=0.05)({})
    finally:
        release.set()


def test_a_prompt_metric_is_untouched() -> None:
    assert wrap_with_timeout(lambda i: {"score": 1.0}, seconds=5)({}) == {
        "score": 1.0
    }


def test_the_metric_s_own_exception_is_not_masked() -> None:
    """A raising metric must still look like a raising metric, not a timeout."""

    def explodes(instance: Any) -> Any:
        raise ValueError("bad metric")

    with pytest.raises(ValueError, match="bad metric"):
        wrap_with_timeout(explodes, seconds=5)({})


def test_every_loaded_metric_is_bounded() -> None:
    """Applied in `CodeMetric` construction, so no caller can build one without
    it -- the same arrangement the retry uses."""
    release = threading.Event()

    def hangs(instance: Any) -> Any:
        release.wait(timeout=30)
        return 1.0

    metric = CodeMetric("hangs", "never", hangs)
    import ambient_quality_agent.tools.metrics.timeout as timeout_mod

    original = timeout_mod.TIMEOUT_SECONDS
    try:
        # The wrapper captured the default at construction, so rebuild it.
        bounded = wrap_with_timeout(hangs, seconds=0.05)
        with pytest.raises(TimeoutError):
            bounded({})
        assert metric.evaluate is not hangs, "construction must wrap it"
    finally:
        timeout_mod.TIMEOUT_SECONDS = original
        release.set()
