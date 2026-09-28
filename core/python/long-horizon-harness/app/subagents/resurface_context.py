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

"""Bubble budget for child permission resurfacing.

A blocking ``delegate`` drives an isolated child runner. When the child needs
approval, the child guard re-raises it on the parent turn (one interactive
approval per ``delegate`` call — an ADK constraint). This async-local budget is
how the child guard knows whether a bubble is still available:

- ``None`` (default) → not inside a child drain → parent context, no resurfacing.
- ``0`` → a child drain with no bubble left (e.g. the post-approval resume pass);
  an ``ask_user`` must deny rather than try to bubble again.
- ``> 0`` → a bubble may be spent to surface one approval to the user.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_BUDGET: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "lha_child_bubble_budget", default=None
)


def bubble_budget() -> int | None:
    return _BUDGET.get()


def try_consume_bubble() -> bool:
    """Spend one bubble if available. Returns False outside a drain or at zero."""
    budget = _BUDGET.get()
    if budget is None or budget <= 0:
        return False
    _BUDGET.set(budget - 1)
    return True


@contextmanager
def child_drain(budget: int) -> Iterator[None]:
    token = _BUDGET.set(budget)
    try:
        yield
    finally:
        _BUDGET.reset(token)


__all__ = ["bubble_budget", "child_drain", "try_consume_bubble"]
