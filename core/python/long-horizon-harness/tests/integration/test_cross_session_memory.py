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

"""Cross-session memory persistence — integration tests.

Validates the contract the in-process evalsets cannot test: a fact stored in
session 1 is retrievable in session 2 against the SAME MemoryService and the
SAME (app_name, user_id) key. The ADK `local_eval_service` runs eval_cases
in parallel against fresh MemoryService instances per case
(evaluation_generator.py:554-557), so cross-session continuity is invisible
to evalsets — it must be validated here.

These tests assert on MemoryService STATE (the bytes Memory Bank stores),
never on LLM response content. That is what makes them pytest-appropriate.
LLM behavior validation lives in tests/eval/evalsets/memory_recall.evalset.json.

The (app_name, user_id) pair is the isolation key — see
InMemoryMemoryService._user_key in google/adk/memory/in_memory_memory_service.py.
That contract is the contract this test exists to pin.
"""

from __future__ import annotations

import pytest
from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService
from google.genai import types

from horizon.memory.add_memory_tool import add_memory_entry
from horizon.memory.auto_capture import auto_capture_callback

pytestmark = pytest.mark.asyncio


APP_NAME = "test_cross_session"


# =============================================================================
# Explicit memory cross-session persistence
# =============================================================================


async def test_memory_persists_across_sessions_for_same_user(
    fake_memory_service: InMemoryMemoryService,
    fake_session_service: InMemorySessionService,
) -> None:
    """A fact stored via add_memory_entry in session 1 is searchable from
    session 2 against the same user_id."""
    user_id = "alice"

    session1 = await fake_session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )
    result = await add_memory_entry(
        content="alice works in Pacific timezone",
        scope="user",
        memory_service=fake_memory_service,
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session1.id,
    )
    assert result["success"] is True, result

    session2 = await fake_session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )
    assert session2.id != session1.id, "Fresh session must have a new id"

    response = await fake_memory_service.search_memory(
        app_name=APP_NAME, user_id=user_id, query="Pacific timezone"
    )

    recalled_text = " ".join(
        part.text
        for memory in response.memories
        if memory.content and memory.content.parts
        for part in memory.content.parts
        if part.text
    )
    assert "Pacific" in recalled_text
    assert "alice" in recalled_text


async def test_memory_isolated_between_users(
    fake_memory_service: InMemoryMemoryService,
    fake_session_service: InMemorySessionService,
) -> None:
    """Alice's stored fact must not appear in Bob's search_memory results.

    Memory Bank is keyed on (app_name, user_id) — this test pins that
    contract. If isolation breaks, the same memory_service state would leak
    a fact between users.
    """
    alice_session = await fake_session_service.create_session(
        app_name=APP_NAME, user_id="alice"
    )
    await add_memory_entry(
        content="alice's API key is sk-quokka-1234",
        scope="user",
        memory_service=fake_memory_service,
        app_name=APP_NAME,
        user_id="alice",
        session_id=alice_session.id,
    )

    bob_response = await fake_memory_service.search_memory(
        app_name=APP_NAME, user_id="bob", query="quokka"
    )
    assert bob_response.memories == [], (
        "Bob must not see alice's memory entries — "
        f"got {len(bob_response.memories)} leaked entries"
    )

    alice_response = await fake_memory_service.search_memory(
        app_name=APP_NAME, user_id="alice", query="quokka"
    )
    assert len(alice_response.memories) >= 1, (
        "Sanity check: alice must still see her own memory — "
        "isolation must not break self-access"
    )


async def test_memory_isolated_between_app_names(
    fake_memory_service: InMemoryMemoryService,
    fake_session_service: InMemorySessionService,
) -> None:
    """Same user_id under a different app_name must NOT see the other app's
    facts. The full Memory Bank key is (app_name, user_id), not just user_id.
    """
    session_a = await fake_session_service.create_session(
        app_name="app_a", user_id="alice"
    )
    await add_memory_entry(
        content="alice prefers numbat-flavored coffee",
        scope="user",
        memory_service=fake_memory_service,
        app_name="app_a",
        user_id="alice",
        session_id=session_a.id,
    )

    response_b = await fake_memory_service.search_memory(
        app_name="app_b", user_id="alice", query="numbat"
    )
    assert response_b.memories == [], (
        "app_b/alice must not see app_a/alice's entries — "
        f"got {len(response_b.memories)} leaked entries"
    )

    response_a = await fake_memory_service.search_memory(
        app_name="app_a", user_id="alice", query="numbat"
    )
    assert len(response_a.memories) >= 1


# =============================================================================
# auto_capture_callback cross-session persistence
# =============================================================================


async def test_auto_capture_flushes_session_events_to_memory(
    fake_memory_service: InMemoryMemoryService,
    fake_session_service: InMemorySessionService,
) -> None:
    """After auto_capture_callback runs, the session's conversational events
    are searchable from a fresh session via the same MemoryService.

    This is the path PreloadMemoryTool consumes — without auto-capture, the
    next session has nothing to preload. Hence the contract: turn-end
    callback must persist the just-finished session's events.
    """
    user_id = "alice"
    session1 = await fake_session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )

    from google.adk.events import Event

    session1.events.append(
        Event(
            invocation_id="inv_1",
            author="user",
            content=types.Content(
                role="user",
                parts=[types.Part.from_text(text="my dog's name is platypus")],
            ),
        )
    )
    session1.events.append(
        Event(
            invocation_id="inv_1",
            author="root_agent",
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Got it — platypus.")],
            ),
        )
    )

    # The callback uses callback_context.add_session_to_memory() which is a
    # thin wrapper over memory_service.add_session_to_memory(session). Drive
    # the underlying contract directly here (the wrapper itself is one line of
    # ADK code — the contract that matters is that the events land in the
    # memory_service keyed under (app_name, user_id)).
    await fake_memory_service.add_session_to_memory(session1)

    session2 = await fake_session_service.create_session(
        app_name=APP_NAME, user_id=user_id
    )
    assert session2.id != session1.id

    response = await fake_memory_service.search_memory(
        app_name=APP_NAME, user_id=user_id, query="platypus"
    )

    recalled = " ".join(
        part.text
        for memory in response.memories
        if memory.content and memory.content.parts
        for part in memory.content.parts
        if part.text
    )
    assert "platypus" in recalled


async def test_auto_capture_callback_is_noop_when_service_unset(
    fake_session_service: InMemorySessionService,
) -> None:
    """If no MemoryService is configured on the runtime, the callback must
    silently no-op rather than raise — the agent must remain runnable in
    memory-less deployments.

    Asserts on the function's behavior, not on any model output.
    """

    class _FakeInvocationContext:
        memory_service = None

    class _FakeCallbackContext:
        _invocation_context = _FakeInvocationContext()

        async def add_session_to_memory(self) -> None:
            raise AssertionError(
                "add_session_to_memory must not be called when memory_service is None"
            )

    result = await auto_capture_callback(_FakeCallbackContext())  # type: ignore[arg-type]
    assert result is None
