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

"""Tests for the clarify tool — ADK 2.0 HITL question/answer surface.

First call → ``tool_context.request_confirmation(hint=question, payload=...)``
plus ``actions.skip_summarization = True`` plus a pending result envelope.
Resume call with a ``ToolConfirmation`` whose payload carries the answer
→ answered envelope. ``confirmed=False`` → declined envelope.

Validation behavior (empty question rejected, choices trimmed to
``MAX_CHOICES=4``, empty list collapses to open-ended) is preserved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _fake_context(
    *,
    tool_confirmation: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        state={},
        tool_confirmation=tool_confirmation,
        actions=SimpleNamespace(skip_summarization=False),
        request_confirmation=MagicMock(),
    )


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_clarify_function():
    from horizon.tools.clarify import clarify

    assert callable(clarify)


def test_max_choices_constant_matches_lha():
    from horizon.tools.clarify import MAX_CHOICES

    assert MAX_CHOICES == 4


# =============================================================================
# Validation
# =============================================================================


def test_empty_question_rejected():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(question="", tool_context=ctx)

    assert result["success"] is False
    assert "question" in result["error"].lower()
    ctx.request_confirmation.assert_not_called()


def test_whitespace_only_question_rejected():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(question="   \n\t ", tool_context=ctx)

    assert result["success"] is False
    ctx.request_confirmation.assert_not_called()


def test_question_is_stripped():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(question="  what color?  ", tool_context=ctx)

    assert result["success"] is True
    assert result["question"] == "what color?"


# =============================================================================
# Choices handling
# =============================================================================


def test_open_ended_question_has_no_choices():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(question="describe your goal", tool_context=ctx)

    assert result["success"] is True
    assert result["choices"] is None


def test_choices_passed_through():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(
        question="pick a color",
        choices=["red", "green", "blue"],
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["choices"] == ["red", "green", "blue"]


def test_choices_trimmed_to_max():
    from horizon.tools.clarify import MAX_CHOICES, clarify

    ctx = _fake_context()
    too_many = [f"opt-{i}" for i in range(MAX_CHOICES + 3)]
    result = clarify(question="pick one", choices=too_many, tool_context=ctx)

    assert result["success"] is True
    assert len(result["choices"]) == MAX_CHOICES
    assert result["choices"] == too_many[:MAX_CHOICES]


def test_blank_choices_filtered():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(
        question="pick",
        choices=["a", "", "  ", "b"],
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["choices"] == ["a", "b"]


def test_all_blank_choices_collapse_to_open_ended():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(
        question="any thoughts?",
        choices=["", "   "],
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["choices"] is None


def test_choices_stripped_individually():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(
        question="pick",
        choices=["  red  ", "\tblue\n"],
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["choices"] == ["red", "blue"]


def test_non_list_choices_rejected():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(
        question="pick",
        choices="not a list",  # type: ignore[arg-type]
        tool_context=ctx,
    )

    assert result["success"] is False
    assert "list" in result["error"].lower()


# =============================================================================
# First call — invokes request_confirmation and returns pending envelope
# =============================================================================


def test_first_call_requests_confirmation_with_question_as_hint():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    clarify(question="continue?", choices=["yes", "no"], tool_context=ctx)

    ctx.request_confirmation.assert_called_once()
    kwargs = ctx.request_confirmation.call_args.kwargs
    assert kwargs["hint"] == "continue?"
    assert kwargs["payload"] == {
        "question": "continue?",
        "choices": ["yes", "no"],
    }


def test_first_call_sets_skip_summarization():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    clarify(question="why?", tool_context=ctx)

    assert ctx.actions.skip_summarization is True


def test_first_call_returns_awaiting_status():
    from horizon.tools.clarify import clarify

    ctx = _fake_context()
    result = clarify(question="why?", tool_context=ctx)

    assert result["status"] == "awaiting_user_response"
    assert result["success"] is True


# =============================================================================
# Resume call — tool_confirmation carries user's answer
# =============================================================================


def test_resume_with_answer_payload_returns_answer():
    from horizon.tools.clarify import clarify

    ctx = _fake_context(
        tool_confirmation=SimpleNamespace(
            confirmed=True, payload={"answer": "yes"}
        )
    )
    result = clarify(
        question="continue?", choices=["yes", "no"], tool_context=ctx
    )

    assert result["success"] is True
    assert result["status"] == "answered"
    assert result["answer"] == "yes"
    ctx.request_confirmation.assert_not_called()


def test_resume_with_choice_payload_returns_answer():
    """Predefined-choice buttons send the picked option under ``choice``."""
    from horizon.tools.clarify import clarify

    ctx = _fake_context(
        tool_confirmation=SimpleNamespace(
            confirmed=True,
            payload={"choice": "Verify the existing install works"},
        )
    )
    result = clarify(
        question="What do you want me to do?",
        choices=["Reinstall", "Verify the existing install works"],
        tool_context=ctx,
    )

    assert result["success"] is True
    assert result["status"] == "answered"
    assert result["answer"] == "Verify the existing install works"


def test_resume_with_string_payload_returns_answer():
    """Front-ends that pass a bare string as the payload still work."""
    from horizon.tools.clarify import clarify

    ctx = _fake_context(
        tool_confirmation=SimpleNamespace(confirmed=True, payload="freeform")
    )
    result = clarify(question="why?", tool_context=ctx)

    assert result["success"] is True
    assert result["answer"] == "freeform"


def test_resume_without_answer_returns_none_answer():
    from horizon.tools.clarify import clarify

    ctx = _fake_context(
        tool_confirmation=SimpleNamespace(confirmed=True, payload=None)
    )
    result = clarify(question="why?", tool_context=ctx)

    assert result["success"] is True
    assert result["answer"] is None


def test_resume_declined_returns_declined_envelope():
    from horizon.tools.clarify import clarify

    ctx = _fake_context(
        tool_confirmation=SimpleNamespace(confirmed=False, payload=None)
    )
    result = clarify(question="continue?", tool_context=ctx)

    assert result["success"] is False
    assert result["status"] == "declined"


# =============================================================================
# tool_context is keyword-only
# =============================================================================


def test_tool_context_is_keyword_only():
    import inspect

    from horizon.tools.clarify import clarify

    sig = inspect.signature(clarify)
    tc = sig.parameters["tool_context"]
    assert tc.kind == inspect.Parameter.KEYWORD_ONLY


# =============================================================================
# Wiring
# =============================================================================


def test_clarify_registered_on_root_agent():
    from horizon.agent import root_agent

    # By name, not identity: the tool list holds the FunctionTool wrappers
    # that carry the compact declaration, not the bare functions.
    names = {
        getattr(t, "name", None) or getattr(t, "__name__", "")
        for t in root_agent.tools
    }
    assert "clarify" in names
