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

"""Deterministic tests for the no-LLM tool-output pruning transform.

We assert only on the event-list mutation (which bodies got zeroed, which were
protected) and the reclaimed-token bookkeeping — never on any model output.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.events.event import Event
from google.genai.types import Content, FunctionResponse, Part

from horizon.context.tool_output_pruning import (
    PRUNE_MARKER,
    prune_tool_outputs,
    prune_tool_outputs_callback,
)


def _tool_event(name: str, body: str, *, ts: float) -> Event:
    part = Part(
        function_response=FunctionResponse(
            id=f"{name}-{ts}", name=name, response={"output": body}
        )
    )
    return Event(
        author="user",
        content=Content(role="user", parts=[part]),
        invocation_id=f"inv-{ts}",
        timestamp=ts,
    )


def _user_event(text: str, *, ts: float) -> Event:
    return Event(
        author="user",
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id=f"user-{ts}",
        timestamp=ts,
    )


def _big(n: int) -> str:
    return "x" * n


def _resp_body(event: Event) -> object:
    return event.content.parts[0].function_response.response


class TestPruneTransform:
    def test_old_large_tool_output_is_zeroed(self) -> None:
        # Three turns; protect the most recent 1 turn. Oldest big read_file
        # output should be pruned.
        events = [
            _tool_event("read_file", _big(80_000), ts=1.0),  # old, big -> prune
            _user_event("next", ts=2.0),
            _tool_event(
                "read_file", _big(80_000), ts=3.0
            ),  # recent -> protected
            _user_event("now", ts=4.0),
        ]
        result = prune_tool_outputs(
            events, protect_recent_turns=1, protect_token_budget=0
        )
        assert result.reclaimed_tokens > 0
        assert result.pruned_count == 1
        assert _resp_body(events[0]) == {"pruned": PRUNE_MARKER}
        # Recent one untouched.
        assert _resp_body(events[2]) == {"output": _big(80_000)}

    def test_skill_outputs_are_never_pruned(self) -> None:
        events = [
            _tool_event("load_skill", _big(80_000), ts=1.0),
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        result = prune_tool_outputs(
            events, protect_recent_turns=0, protect_token_budget=0
        )
        assert result.pruned_count == 0
        assert _resp_body(events[0]) == {"output": _big(80_000)}

    def test_subagent_and_clarify_outputs_are_never_pruned(self) -> None:
        # "re-run the tool if needed" means re-running a multi-minute,
        # multi-dollar agent, or re-interrupting the user — neither is a
        # sane recovery for a pruned part.
        from horizon.tools import names

        events = [
            _tool_event(names.SUBAGENT, _big(80_000), ts=1.0),
            _tool_event(names.CLARIFY, _big(80_000), ts=1.5),
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        result = prune_tool_outputs(
            events, protect_recent_turns=0, protect_token_budget=0
        )
        assert result.pruned_count == 0
        assert _resp_body(events[0]) == {"output": _big(80_000)}
        assert _resp_body(events[1]) == {"output": _big(80_000)}

    def test_overflow_path_survives_pruning(self) -> None:
        # read/bash spill oversized output to disk and return a pointer key
        # ending in overflow_path; pruning must not delete the only way
        # back to that file.
        part = Part(
            function_response=FunctionResponse(
                id="read-1",
                name="read",
                response={
                    "content": _big(80_000),
                    "overflow_path": "lha/tool-output/stdout-abc.txt",
                },
            )
        )
        old_event = Event(
            author="user",
            content=Content(role="user", parts=[part]),
            invocation_id="inv-1",
            timestamp=1.0,
        )
        events = [
            old_event,
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        result = prune_tool_outputs(
            events, protect_recent_turns=0, protect_token_budget=0
        )
        assert result.pruned_count == 1
        body = _resp_body(events[0])
        assert body["pruned"] == PRUNE_MARKER
        assert body["overflow_path"] == "lha/tool-output/stdout-abc.txt"

    def test_small_outputs_below_floor_not_pruned(self) -> None:
        events = [
            _tool_event("read_file", "tiny", ts=1.0),
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        result = prune_tool_outputs(
            events,
            protect_recent_turns=0,
            protect_token_budget=0,
            min_part_tokens=1_000,
        )
        assert result.pruned_count == 0

    def test_no_change_when_reclaim_below_minimum(self) -> None:
        # One prunable part just over min_part_tokens but under min_reclaim:
        # nothing is mutated and reclaimed is reported as 0.
        events = [
            _tool_event("read_file", _big(5_000), ts=1.0),  # ~1250 tokens
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        result = prune_tool_outputs(
            events,
            protect_recent_turns=0,
            protect_token_budget=0,
            min_part_tokens=100,
            min_reclaim_tokens=100_000,
        )
        assert result.pruned_count == 0
        assert result.reclaimed_tokens == 0
        assert _resp_body(events[0]) == {"output": _big(5_000)}

    def test_already_pruned_part_is_skipped(self) -> None:
        events = [
            _tool_event("read_file", _big(80_000), ts=1.0),
            _user_event("turn", ts=2.0),
            _user_event("turn2", ts=3.0),
        ]
        prune_tool_outputs(
            events, protect_recent_turns=0, protect_token_budget=0
        )
        # Second pass: nothing left to reclaim.
        result = prune_tool_outputs(
            events, protect_recent_turns=0, protect_token_budget=0
        )
        assert result.pruned_count == 0
        assert result.reclaimed_tokens == 0

    def test_protect_token_budget_keeps_recent_bodies(self) -> None:
        # Large budget keeps even old bodies because their cumulative estimate
        # stays within the protected budget.
        events = [
            _tool_event("read_file", _big(40_000), ts=1.0),
            _user_event("turn", ts=2.0),
            _tool_event("read_file", _big(40_000), ts=3.0),
            _user_event("turn2", ts=4.0),
        ]
        result = prune_tool_outputs(
            events,
            protect_recent_turns=0,
            protect_token_budget=1_000_000,
        )
        assert result.pruned_count == 0

    def test_ordinary_session_of_small_calls_is_reclaimed_at_default_floor(
        self,
    ) -> None:
        """Regression for the measured blindness: with the old 2,000-token
        floor, a completely ordinary session of 30 turns x 2 tool calls at
        ~4KB each (a realistic read/bash mix, ~1,000 estimated tokens per
        part) reclaimed exactly zero, because no single part ever crossed
        the floor to become a candidate. Uses real user-turn events (a
        prior probe without them reported a false zero everywhere, since
        `_is_user_turn` requires a text-only part and `turns_seen` never
        advanced). Asserts against the module's PRODUCTION DEFAULTS, not
        overridden thresholds, so this fails again if the floor regresses.
        """
        ts = 0.0
        events: list[Event] = []
        for i in range(30):
            ts += 1
            events.append(_user_event(f"turn {i}", ts=ts))
            for _ in range(2):
                ts += 1
                events.append(_tool_event("bash", _big(4_000), ts=ts))

        result = prune_tool_outputs(events)  # production defaults

        assert result.pruned_count > 0, (
            "An ordinary 60-tool-call session accumulating 240,000 chars "
            "reclaimed nothing at the default min_part_tokens floor."
        )
        assert result.reclaimed_tokens > 0

    def test_recent_and_protected_window_still_untouched_at_default_floor(
        self,
    ) -> None:
        """Lowering the floor must not start pruning the recent/protected
        window it was never meant to touch — only stale mid-session output
        that was previously invisible to the pruner."""
        events: list[Event] = []
        ts = 0.0
        for i in range(3):  # within DEFAULT_PROTECT_RECENT_TURNS
            ts += 1
            events.append(_user_event(f"turn {i}", ts=ts))
            for _ in range(2):
                ts += 1
                events.append(_tool_event("bash", _big(4_000), ts=ts))

        result = prune_tool_outputs(events)  # production defaults

        assert result.pruned_count == 0
        assert result.reclaimed_tokens == 0


def _prunable_session_events() -> list[Event]:
    # The callback runs with production defaults (protect 3 recent turns, 40k
    # token budget). To actually prune, the oldest tool body must sit beyond
    # the recent-turn window with enough newer tool volume ahead of it to
    # exhaust the protected budget.
    return [
        _tool_event("read_file", _big(240_000), ts=1.0),  # old target -> prune
        _user_event("a", ts=2.0),
        _tool_event("read_file", _big(60_000), ts=3.0),
        _user_event("b", ts=4.0),
        _tool_event("read_file", _big(60_000), ts=5.0),
        _user_event("c", ts=6.0),
        _tool_event("read_file", _big(60_000), ts=7.0),
        _user_event("d", ts=8.0),
    ]


@pytest.mark.asyncio
class TestPruneCallback:
    async def test_mutates_session_events(self) -> None:
        events = _prunable_session_events()
        session = SimpleNamespace(events=events)
        ctx = SimpleNamespace(
            _invocation_context=SimpleNamespace(session=session)
        )
        await prune_tool_outputs_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )
        assert events[0].content.parts[0].function_response.response == {
            "pruned": PRUNE_MARKER
        }

    async def test_disabled_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LHA_PRUNE_TOOL_OUTPUTS", "0")
        events = _prunable_session_events()
        session = SimpleNamespace(events=events)
        ctx = SimpleNamespace(
            _invocation_context=SimpleNamespace(session=session)
        )
        await prune_tool_outputs_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )
        assert events[0].content.parts[0].function_response.response == {
            "output": _big(240_000)
        }

    async def test_missing_context_is_noop(self) -> None:
        ctx = SimpleNamespace(_invocation_context=None)
        await prune_tool_outputs_callback(
            callback_context=ctx, llm_request=SimpleNamespace()
        )
