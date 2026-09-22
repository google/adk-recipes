# Copyright 2025 Google LLC
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

"""Opt-in transcript handoff: a child sees only what it is handed, unless
the caller asks for the conversation. Copies TEXT only, never the event
stream, because a child declares fewer tools than the parent and replaying
function_calls it cannot make is malformed history."""

from types import SimpleNamespace
from typing import Any

import pytest

from horizon.subagents.transcript import (
    MAX_TRANSCRIPT_CHARS,
    parent_transcript,
)

pytestmark = pytest.mark.asyncio


def _part(text: str | None = None, fn: str | None = None) -> Any:
    return SimpleNamespace(
        text=text,
        function_call=SimpleNamespace(name=fn) if fn else None,
        function_response=None,
    )


def _event(author: str, *parts: Any) -> Any:
    return SimpleNamespace(
        author=author, content=SimpleNamespace(parts=list(parts))
    )


async def test_returns_empty_when_not_opted_in() -> None:
    assert parent_transcript(None) == ""


async def test_copies_user_and_model_text() -> None:
    events = [
        _event("user", _part("deploy is failing on staging")),
        _event("agent", _part("checked the logs, it is a locale mismatch")),
    ]
    out = parent_transcript(events)
    assert "deploy is failing on staging" in out
    assert "locale mismatch" in out


async def test_drops_tool_calls_and_their_payloads() -> None:
    """The child declares fewer tools; a copied `function_call` to a tool it
    does not have is malformed history, and tool payloads are the bulk the
    pruner exists to remove."""
    events = [
        _event("user", _part("read the config")),
        _event("agent", _part(fn="read")),
        _event("agent", _part("config sets LANG=C.UTF-8")),
    ]
    out = parent_transcript(events)
    assert "config sets LANG=C.UTF-8" in out
    assert "read" not in out.replace("read the config", "")


async def test_truncates_oldest_first_under_the_cap() -> None:
    events = [_event("user", _part("OLDEST"))]
    events += [
        _event("user", _part("x" * 500)) for _ in range(MAX_TRANSCRIPT_CHARS)
    ]
    events.append(_event("user", _part("NEWEST")))

    out = parent_transcript(events)

    assert len(out) <= MAX_TRANSCRIPT_CHARS
    assert "NEWEST" in out, "must keep the most recent turns"
    assert "OLDEST" not in out, "must drop the oldest turns first"


async def test_skips_events_with_no_content() -> None:
    events = [
        SimpleNamespace(author="agent", content=None),
        _event("user", _part("still here")),
    ]
    assert "still here" in parent_transcript(events)


async def test_subagent_off_by_default(monkeypatch) -> None:
    """A child sees only what it is handed. Default must not leak the chat."""
    from horizon.subagents import subagent as mod

    seen: dict[str, Any] = {}

    async def fake_delegate(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "completed", "summary": "ok"}

    monkeypatch.setattr(mod, "delegate", fake_delegate)
    await mod.subagent(goal="g", context="briefed", tool_context=None)

    assert seen["context"] == "briefed"


async def test_subagent_prepends_transcript_when_opted_in(monkeypatch) -> None:
    from horizon.subagents import subagent as mod

    seen: dict[str, Any] = {}

    async def fake_delegate(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "completed", "summary": "ok"}

    monkeypatch.setattr(mod, "delegate", fake_delegate)
    monkeypatch.setattr(
        mod, "_parent_events", lambda _ctx: [_event("user", _part("earlier"))]
    )

    await mod.subagent(
        goal="g", context="briefed", include_transcript=True, tool_context=None
    )

    assert "earlier" in seen["context"], seen["context"]
    assert "briefed" in seen["context"], "explicit briefing must survive"


async def test_awaiting_approval_summary_says_nothing_is_done() -> None:
    """The paused-for-approval envelope had `summary` set to the bare hint,
    which reads like a result. An eval caught the parent claiming a paused
    child had created a test suite."""
    from horizon.subagents.delegate import _awaiting_summary

    out = _awaiting_summary("child wants to run `rm -rf build/`")

    assert "PAUSED" in out
    assert "none of the work" in out
    assert "rm -rf build/" in out, "the actual ask must still reach the user"
