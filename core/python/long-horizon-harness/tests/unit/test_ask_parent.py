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

"""``ask_parent`` — a child's one-shot escalation to the parent turn."""

from __future__ import annotations

from app.subagents.ask_parent import ask_parent
from app.subagents.resurface_context import child_drain


class _Actions:
    skip_summarization = False


class _Conf:
    def __init__(self, confirmed: bool, payload=None) -> None:
        self.confirmed = confirmed
        self.payload = payload


class _TC:
    def __init__(self, tool_confirmation=None) -> None:
        self.tool_confirmation = tool_confirmation
        self.actions = _Actions()
        self.requested: list = []

    def request_confirmation(self, *, hint, payload=None) -> None:
        self.requested.append((hint, payload))


def test_empty_question_rejected():
    out = ask_parent("   ", tool_context=_TC())
    assert out["success"] is False


def test_headless_no_budget_returns_no_parent_without_bubbling():
    # Outside a child_drain, bubble_budget() is None -> no parent to ask.
    tc = _TC()
    out = ask_parent("which region?", tool_context=tc)
    assert out["answer"] is None
    assert "note" in out
    assert tc.requested == []


def test_bubbles_when_budget_available():
    tc = _TC()
    with child_drain(1):
        out = ask_parent("which region?", tool_context=tc)
    assert out["status"] == "awaiting_parent"
    assert tc.requested and tc.requested[0][0] == "which region?"
    assert tc.requested[0][1]["kind"] == "ask_parent"


def test_budget_zero_does_not_bubble():
    tc = _TC()
    with child_drain(0):
        out = ask_parent("x?", tool_context=tc)
    assert out["answer"] is None
    assert tc.requested == []


def test_returns_free_text_answer_on_resume():
    tc = _TC(
        tool_confirmation=_Conf(
            confirmed=True, payload={"answer": "us-central1"}
        )
    )
    out = ask_parent("which region?", tool_context=tc)
    assert out["answer"] == "us-central1"


def test_declined_answer():
    tc = _TC(tool_confirmation=_Conf(confirmed=False))
    out = ask_parent("x?", tool_context=tc)
    assert out["answer"] is None
    assert "declined" in out["note"].lower()
