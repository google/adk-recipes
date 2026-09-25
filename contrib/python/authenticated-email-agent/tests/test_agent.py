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

"""Configuration and safety-contract tests for the email agent."""

import importlib
import os

import pytest

os.environ.setdefault("E2A_API_KEY", "synthetic-agent-scoped-key")
os.environ.setdefault("GOOGLE_API_KEY", "synthetic-google-key")
os.environ.setdefault("MODEL_NAME", "gemini-3.5-flash")

agent = importlib.import_module("app.agent")
prompt = importlib.import_module("app.prompt")


def test_agent_uses_hosted_e2a_mcp_with_minimal_email_tools() -> None:
    """Keep the recipe scoped to the hosted runtime email surface."""
    assert agent.E2A_MCP_URL == "https://api.e2a.dev/mcp"
    assert agent.E2A_TOOL_FILTER == (
        "whoami",
        "list_messages",
        "get_message",
        "send_message",
        "reply_to_message",
    )
    assert agent.root_agent is not None
    assert len(agent.root_agent.tools) == 1

    toolset = agent.root_agent.tools[0]
    assert toolset.tool_filter == list(agent.E2A_TOOL_FILTER)
    assert agent._authorization_headers() == {
        "Authorization": "Bearer synthetic-agent-scoped-key"
    }
    assert agent.root_agent.instruction == prompt.EMAIL_AGENT_INSTRUCTION


def test_missing_e2a_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not create an unauthenticated MCP connection by accident."""
    monkeypatch.delenv("E2A_API_KEY")

    with pytest.raises(KeyError, match="E2A_API_KEY"):
        agent._authorization_headers()


def test_instruction_preserves_threads_and_treats_email_as_untrusted() -> None:
    """Pin the safety and threading rules shown by this recipe."""
    instruction = prompt.EMAIL_AGENT_INSTRUCTION

    for required_text in (
        "untrusted",
        "credential scope is `agent`",
        "explicit instruction",
        "verify a domain, not a person",
        "list_messages",
        "get_message",
        "send_message",
        "reply_to_message",
        "In-Reply-To",
        "References",
        "idempotency_key",
        "scheduled",
        "sent",
        "pending_review",
        "review_approved",
        "failed",
        "open-ended",
        "unknown status",
        "do not retry",
    ):
        assert required_text in instruction
