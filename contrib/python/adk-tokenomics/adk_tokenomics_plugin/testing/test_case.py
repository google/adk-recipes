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

"""
Tokenomics SDK - Unittest Base Class for Automated Agent Token Regression Testing
"""

import unittest
from contextlib import contextmanager

from adk_tokenomics_plugin.core.config import TokenomicsConfig
from adk_tokenomics_plugin.core.sinks import InMemorySink
from adk_tokenomics_plugin.core.tracker import TokenTracker
from adk_tokenomics_plugin.testing.assertions import (
    assert_cache_ratio_above,
    assert_cost_under,
    assert_thinking_budget,
    assert_token_budget,
)


class TokenomicsTestCase(unittest.TestCase):
    """
    Base test case equipped with isolated TokenTracker and assertion helpers.

    Usage:
        class TestMyAgent(TokenomicsTestCase):
            def test_summary_workflow(self):
                with self.track_tokens() as tracker:
                    # Run agent turn
                    tracker.record_turn(...)
                    self.assertCostLessThan(tracker, max_dollars=0.005)
                    self.assertCacheHitRatioGreaterThan(tracker, min_ratio_pct=50.0)
    """

    def setUp(self):
        super().setUp()
        self.config = TokenomicsConfig(mode="test", sink="memory")
        self.sink = InMemorySink()
        self.tracker = TokenTracker(config=self.config, sink=self.sink)

    @contextmanager
    def track_tokens(self):
        """Context manager yielding an isolated TokenTracker + InMemorySink for a test block."""
        scoped_sink = InMemorySink()
        scoped_tracker = TokenTracker(config=self.config, sink=scoped_sink)
        yield scoped_tracker

    def assertCostLessThan(
        self, tracker: TokenTracker, max_dollars: float, msg: str = ""
    ):
        assert_cost_under(tracker, max_dollars, msg)

    def assertCacheHitRatioGreaterThan(
        self,
        tracker: TokenTracker,
        min_ratio_pct: float | None = None,
        msg: str = "",
        *,
        min_ratio: float | None = None,
    ):
        threshold = min_ratio_pct if min_ratio_pct is not None else min_ratio
        if threshold is None:
            raise TypeError(
                "assertCacheHitRatioGreaterThan() missing required argument: 'min_ratio_pct'"
            )
        assert_cache_ratio_above(tracker, threshold, msg)

    def assertTokenBudget(
        self, tracker: TokenTracker, max_tokens: int, msg: str = ""
    ):
        assert_token_budget(tracker, max_tokens, msg)

    def assertThinkingTokensWithin(
        self,
        tracker: TokenTracker,
        min_tokens: int = 0,
        max_tokens: int = 8192,
        msg: str = "",
    ):
        assert_thinking_budget(tracker, min_tokens, max_tokens, msg)
