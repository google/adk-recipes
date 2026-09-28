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

"""Graceful-halt helper contract — request mutation only, no LLM assertions."""

from __future__ import annotations

from google.adk.models import LlmRequest
from google.genai import types

from app.conversation.graceful_halt import (
    HANDOFF_MARKER,
    apply_graceful_halt,
)


def _request_with_tools() -> LlmRequest:
    req = LlmRequest()
    req.tools_dict = {"terminal": object(), "read_file": object()}
    req.config.tools = [types.Tool(function_declarations=[])]
    return req


def test_strips_tools_dict():
    req = _request_with_tools()
    apply_graceful_halt(req, reason="iteration limit reached: 50")
    assert req.tools_dict == {}


def test_strips_config_tools():
    req = _request_with_tools()
    apply_graceful_halt(req, reason="iteration limit reached: 50")
    assert not req.config.tools


def test_appends_handoff_reminder_at_tail():
    req = _request_with_tools()
    req.contents = [types.Content(role="user", parts=[types.Part(text="go")])]
    apply_graceful_halt(req, reason="iteration limit reached: 50")
    tail = req.contents[-1]
    assert tail.role == "user"
    text = tail.parts[0].text
    assert HANDOFF_MARKER in text
    assert "iteration limit reached: 50" in text
    assert "summary" in text.lower()
    assert "remaining" in text.lower()
    assert "recommend" in text.lower()


def test_reason_included_but_no_tool_promise():
    req = _request_with_tools()
    req.contents = []
    apply_graceful_halt(req, reason="agent halted")
    text = req.contents[-1].parts[0].text
    assert "agent halted" in text
    # Must instruct text-only — no further tool use.
    assert "text only" in text.lower() or "do not" in text.lower()
