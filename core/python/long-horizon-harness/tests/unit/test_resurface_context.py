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

"""Bubble-budget ContextVar for child permission resurfacing."""

from __future__ import annotations

from app.subagents.resurface_context import (
    bubble_budget,
    child_drain,
    try_consume_bubble,
)


def test_default_budget_is_none_outside_a_drain() -> None:
    assert bubble_budget() is None
    assert try_consume_bubble() is False


def test_child_drain_sets_and_resets_budget() -> None:
    assert bubble_budget() is None
    with child_drain(1):
        assert bubble_budget() == 1
    assert bubble_budget() is None


def test_consume_decrements_once() -> None:
    with child_drain(1):
        assert try_consume_bubble() is True
        assert bubble_budget() == 0
        assert try_consume_bubble() is False
        assert bubble_budget() == 0


def test_zero_budget_never_consumes() -> None:
    with child_drain(0):
        assert bubble_budget() == 0
        assert try_consume_bubble() is False
