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

"""Regression test for IterationBudgetPlugin with eval user and > 5 iterations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.conversation.iteration_budget_plugin import (
    IterationBudgetPlugin,
)


def _fake_invocation_context(
    *, user_id: str = "eval_alice", session_id: str = "s1"
) -> SimpleNamespace:
    """Duck-typed InvocationContext exposing only the fields the plugin reads."""
    state: dict[str, Any] = {}
    session = SimpleNamespace(id=session_id, state=state, user_id=user_id)
    return SimpleNamespace(user_id=user_id, session=session)


@pytest.mark.asyncio
async def test_iteration_budget_eval_user_regression():
    """Verify IterationBudgetPlugin behavior when running > 5 iterations for an eval user."""
    plugin = IterationBudgetPlugin(max_iterations=10)
    ctx = _fake_invocation_context(user_id="eval_alice")

    # Run 6 iterations
    for i in range(1, 7):
        await plugin.before_run_callback(invocation_context=ctx)
        assert ctx.session.state["iteration"] == i
