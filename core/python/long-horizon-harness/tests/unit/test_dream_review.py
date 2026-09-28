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

"""Dream-review pass — server-side profile generation via Memory Bank.

``_run_dream_review_for_user`` collects a user's recent session events and
hands them to ``memories.generate`` so Memory Bank consolidates the user's
Structured Profile. When prerequisites are missing it returns a structured
``{success: false, reason: ...}`` envelope instead of raising. Discovery
helpers (``list_active_users`` / ``_collect_user_sessions``) are covered in
``test_dream_users.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.adk.events.event import Event
from google.adk.memory import InMemoryMemoryService
from google.genai.types import Blob, Content, Part

pytestmark = pytest.mark.asyncio


def _make_event(text: str, *, author: str = "user") -> Event:
    return Event(
        author=author,
        content=Content(role="user", parts=[Part(text=text)]),
        invocation_id="inv-test",
    )


def _make_json_attachment_event() -> Event:
    """A user turn with text plus a JSON file attachment (the production
    shape that 400s Memory Bank's extractor)."""
    return Event(
        author="user",
        content=Content(
            role="user",
            parts=[
                Part(text="here is my config"),
                Part(
                    inline_data=Blob(
                        mime_type="application/json", data=b'{"a":1}'
                    )
                ),
            ],
        ),
        invocation_id="inv-test",
    )


async def test_generate_receives_no_unsupported_inline_mime(fake_vertex):
    """JSON inline_data is degraded to text before reaching generate, so the
    POST body can never carry the mimeType the extractor rejects."""
    from app.memory import dream_review

    service = _FakeSessionService([[_make_json_attachment_event()]])
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=service,
    )

    assert result["success"] is True
    call = fake_vertex.generate_calls[0]
    sent = call["events"]
    assert len(sent) == 1
    # No part forwarded to generate may carry inline_data with a mime the
    # extractor rejects.
    for ev in sent:
        for part in ev.content.parts:
            blob = getattr(part, "inline_data", None)
            if blob is not None:
                mime = (blob.mime_type or "").lower()
                assert mime.startswith("image/") or mime == "application/pdf"
    # The text part survives.
    assert any(
        getattr(p, "text", None) and "config" in p.text
        for p in sent[0].content.parts
    )


class _FakeSessionService:
    """Duck-typed session service: lists sessions, loads their events."""

    def __init__(
        self, sessions: list[list[Event]], *, user_id: str = "user-1"
    ) -> None:
        self._sessions = {
            f"s{i}": SimpleNamespace(
                id=f"s{i}",
                user_id=user_id,
                last_update_time=float(i),
                events=events,
            )
            for i, events in enumerate(sessions)
        }

    async def list_sessions(
        self, *, app_name: str, user_id: str | None = None
    ) -> Any:
        del app_name
        return SimpleNamespace(
            sessions=[
                SimpleNamespace(
                    id=s.id,
                    user_id=s.user_id,
                    last_update_time=s.last_update_time,
                )
                for s in self._sessions.values()
                if user_id is None or s.user_id == user_id
            ]
        )

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> Any:
        del app_name, user_id
        return self._sessions.get(session_id)


class _FakeMemories:
    def __init__(self, actions: list[str] | None = None) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self._actions = actions or ["CREATED", "UPDATED", "DELETED"]

    async def generate(
        self,
        *,
        name,
        direct_contents_source=None,
        scope=None,
        config=None,
        **_kw,
    ) -> Any:
        self.generate_calls.append(
            {
                "name": name,
                "scope": scope,
                "events": list(
                    getattr(direct_contents_source, "events", []) or []
                ),
                "config": config,
            }
        )
        generated = [
            SimpleNamespace(action=SimpleNamespace(name=a))
            for a in self._actions
        ]
        return SimpleNamespace(
            done=True, response=SimpleNamespace(generated_memories=generated)
        )


def _vertex_adapter(client: Any) -> Any:
    from app.memory.adapter import VertexMemoryAdapter

    service = SimpleNamespace(
        _get_api_client=lambda: client,
        _agent_engine_id="eng-1",
        _project=None,
        _location=None,
    )
    return VertexMemoryAdapter(service)


@pytest.fixture
def fake_vertex(monkeypatch):
    """Patch dream_review.memory_adapter → a Vertex adapter over a fake client."""
    from app.memory import dream_review

    memories = _FakeMemories()
    client = SimpleNamespace(agent_engines=SimpleNamespace(memories=memories))
    monkeypatch.setattr(
        dream_review, "memory_adapter", lambda _ms: _vertex_adapter(client)
    )
    return memories


def _ctx(memory: Any, session_service: Any, *, user_id: str = "user-1") -> Any:
    return SimpleNamespace(
        _invocation_context=SimpleNamespace(
            memory_service=memory,
            session_service=session_service,
            app_name="parent",
            user_id=user_id,
        )
    )


# =============================================================================
# Module surface
# =============================================================================


async def test_module_exposes_entrypoints():
    from app.memory import dream_review

    assert callable(dream_review.request_dream_review)
    assert callable(dream_review.list_active_users)
    assert callable(dream_review._run_dream_review_for_user)
    assert dream_review.DREAM_REVIEW_ENABLED_ENV == "LHA_DREAM_REVIEW"


# =============================================================================
# Graceful fallbacks
# =============================================================================


async def test_disabled_env_returns_disabled_envelope(monkeypatch, fake_vertex):
    from app.memory import dream_review

    monkeypatch.setenv(dream_review.DREAM_REVIEW_ENABLED_ENV, "0")
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="alice",
        session_service=_FakeSessionService([[_make_event("hi")]]),
    )
    assert result["success"] is False
    assert "disabled" in result["reason"].lower()
    assert fake_vertex.generate_calls == []


async def test_returns_failure_without_session_service(fake_vertex):
    from app.memory import dream_review

    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="alice",
        session_service=None,
    )
    assert result["success"] is False
    assert "session" in result["reason"].lower()


async def test_returns_failure_for_non_vertex_memory(monkeypatch):
    """Without a Vertex Memory Bank, structured profiles are unavailable."""
    from app.memory import dream_review

    result = await dream_review._run_dream_review_for_user(
        memory_service=InMemoryMemoryService(),
        app_name="parent",
        user_id="alice",
        session_service=_FakeSessionService([[_make_event("hi")]]),
    )
    assert result["success"] is False
    assert "vertex" in result["reason"].lower()


async def test_returns_failure_when_session_service_lacks_lookup(fake_vertex):
    from app.memory import dream_review

    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="alice",
        session_service=SimpleNamespace(),  # no list_sessions/get_session
    )
    assert result["success"] is False
    assert "session" in result["reason"].lower()
    assert fake_vertex.generate_calls == []


async def test_returns_failure_when_no_session_content(fake_vertex):
    from app.memory import dream_review

    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=_FakeSessionService(
            [[], []]
        ),  # sessions, but no events
    )
    assert result["success"] is False
    assert "content" in result["reason"].lower()
    assert fake_vertex.generate_calls == []


# =============================================================================
# Happy path — server-side generate
# =============================================================================


async def test_generate_called_with_scope_and_events(fake_vertex):
    from app.memory import dream_review

    service = _FakeSessionService(
        [[_make_event("first")], [_make_event("second")]]
    )
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=service,
    )
    assert result["success"] is True
    assert result["sessions_reviewed"] == 2
    assert len(fake_vertex.generate_calls) == 1
    call = fake_vertex.generate_calls[0]
    assert call["name"] == "reasoningEngines/eng-1"
    assert call["scope"] == {"app_name": "parent", "user_id": "user-1"}
    # Both sessions' events are forwarded.
    assert len(call["events"]) == 2
    # wait_for_completion so the cron surfaces failures.
    assert getattr(call["config"], "wait_for_completion", None) is True
    assert getattr(call["config"], "disable_consolidation", None) is False


async def test_envelope_reports_consolidation_counts(fake_vertex):
    from app.memory import dream_review

    service = _FakeSessionService([[_make_event("hi")]])
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=service,
    )
    assert result["success"] is True
    assert result["consolidation"] == {
        "ran": True,
        "created": 1,
        "updated": 1,
        "deleted": 1,
    }


async def test_kill_switch_off_disables_consolidation(monkeypatch, fake_vertex):
    from app.memory import dream_review

    monkeypatch.setenv("LHA_MEMORY_CONSOLIDATION", "0")
    service = _FakeSessionService([[_make_event("hi")]])
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=service,
    )
    assert result["success"] is True
    assert result["consolidation"] == {"ran": False, "reason": "disabled"}
    call = fake_vertex.generate_calls[0]
    assert getattr(call["config"], "disable_consolidation", None) is True


async def test_generate_failure_returns_envelope(monkeypatch):
    from app.memory import dream_review

    async def boom(**_kw):
        raise RuntimeError("vertex down")

    memories = SimpleNamespace(generate=boom)
    client = SimpleNamespace(agent_engines=SimpleNamespace(memories=memories))
    monkeypatch.setattr(
        dream_review, "memory_adapter", lambda _ms: _vertex_adapter(client)
    )
    result = await dream_review._run_dream_review_for_user(
        memory_service=object(),
        app_name="parent",
        user_id="user-1",
        session_service=_FakeSessionService([[_make_event("hi")]]),
    )
    assert result["success"] is False
    assert "generation failed" in result["reason"].lower()


# =============================================================================
# request_dream_review wrapper
# =============================================================================


async def test_request_dream_review_threads_context(fake_vertex):
    from app.memory import dream_review

    service = _FakeSessionService([[_make_event("hi")]])
    result = await dream_review.request_dream_review(
        tool_context=_ctx(object(), service)
    )
    assert result["success"] is True
    assert len(fake_vertex.generate_calls) == 1
    assert fake_vertex.generate_calls[0]["scope"] == {
        "app_name": "parent",
        "user_id": "user-1",
    }


async def test_request_dream_review_without_invocation_context():
    from app.memory import dream_review

    result = await dream_review.request_dream_review(
        tool_context=SimpleNamespace(_invocation_context=None)
    )
    assert result["success"] is False
