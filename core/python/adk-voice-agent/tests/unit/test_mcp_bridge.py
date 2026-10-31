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

"""Unit tests for the authenticated ADK Voice Agent MCP Bridge (`app/api/mcp.py`) and `.mcp.json` 1P wiring."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import mcp_gateway
from app.api.mcp import (
    MCP_TOOLS,
    clear_mcp_tokens,
    mcp_env_for_thread,
    mint_mcp_token,
    revoke_mcp_token,
    verify_mcp_token,
)
from app.fast_api_app import app
from app.memory.episodes import set_episode_store
from app.memory.store import set_memory_store
from app.threads.model import (
    EventKind,
    ThreadHandle,
    ThreadState,
)
from app.threads.registry import ThreadRegistry, set_thread_registry
from tests.fakes import FakeEpisodeStore, FakeMemoryStore

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_mcp_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ThreadRegistry]:
    # No connected-app upstreams here (tests/unit/test_mcp_gateway.py covers them).
    monkeypatch.setattr(mcp_gateway, "MCP_JSON_PATH", tmp_path / "none.json")
    clear_mcp_tokens()
    registry = ThreadRegistry()
    set_thread_registry(registry)
    yield registry
    clear_mcp_tokens()
    set_thread_registry(None)


def _add_thread(
    registry: ThreadRegistry,
    *,
    thread_id: str = "thread-mcp12345",
    owner: str = "local",
    title: str = "Refactor auth service",
    goal: str = "Extract OAuth token refresh into a shared helper",
    repo: str = "voice-agent-fixture",
    harness: str = "claude",
    state: ThreadState = ThreadState.EXECUTING,
    result_text: str = "Completed the OAuth helper refactor across 3 modules.",
) -> ThreadHandle:
    handle = ThreadHandle(
        thread_id=thread_id,
        goal=goal,
        title=title,
        project=f"code/{repo}",
        harness=harness,
        branch=f"agent/{thread_id}",
        owner=owner,
        state=state,
        result_text=result_text,
        artifacts=[
            {
                "name": "notes.md",
                "title": "Design Notes",
                "content": "# OAuth Refactor\nAvoid duplicate token refreshes.",
            }
        ],
    )
    handle.record(EventKind.PROGRESS, "Inspecting app/auth/oauth.py")
    registry._threads[thread_id] = handle
    return handle


def test_token_minting_revocation_and_env_for_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = mcp_env_for_thread(
        "thread-abc12345",
        "ada@example.com",
        base_url="https://voice-agent.example.com/",
    )
    assert (
        env["VOICE_AGENT_MCP_URL"]
        == "https://voice-agent.example.com/api/v1/mcp"
    )
    assert env["VOICE_AGENT_THREAD_ID"] == "thread-abc12345"
    token = env["VOICE_AGENT_MCP_TOKEN"]
    assert token.startswith("voice_agent_mcp_")

    grant = verify_mcp_token(token)
    assert grant is not None
    assert grant.thread_id == "thread-abc12345"
    assert grant.user_id == "ada@example.com"

    second_token = mint_mcp_token("thread-abc12345", "ada@example.com")
    assert second_token != token
    assert verify_mcp_token(token) is None
    assert verify_mcp_token(second_token) is not None

    revoke_mcp_token("thread-abc12345")
    assert verify_mcp_token(second_token) is None

    monkeypatch.setenv("VOICE_AGENT_MCP_DEV_TOKEN", "local-dev-secret")
    dev_grant = verify_mcp_token(
        "local-dev-secret", thread_id_hint="thread-dev01"
    )
    assert dev_grant is not None
    assert dev_grant.thread_id == "thread-dev01"
    assert dev_grant.user_id == "local"

    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    assert verify_mcp_token("local-dev-secret") is None


def test_unauthorized_requests_are_rejected() -> None:
    assert (
        client.post(
            "/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        ).status_code
        == 401
    )
    assert client.get("/api/v1/mcp/tools").status_code == 401
    assert (
        client.post(
            "/api/v1/mcp/call", json={"tool": "list_threads", "arguments": {}}
        ).status_code
        == 401
    )

    bad_headers = {"Authorization": "Bearer not-a-real-token"}
    assert (
        client.post(
            "/api/v1/mcp",
            headers=bad_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ).status_code
        == 401
    )

    token = mint_mcp_token("thread-revoked", "local")
    revoke_mcp_token("thread-revoked")
    assert (
        client.get(
            "/api/v1/mcp/tools",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 401
    )


def test_initialize_and_tools_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("VOICE_AGENT_AUTH_MODE", "iap")
    monkeypatch.setenv(
        "VOICE_AGENT_IAP_AUDIENCE", "/projects/1/services/voice-agent"
    )

    token = mint_mcp_token("thread-init01", "user@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    init_res = client.post(
        "/api/v1/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
    )
    assert init_res.status_code == 200
    init_body = init_res.json()
    assert init_body["jsonrpc"] == "2.0"
    assert init_body["id"] == 1
    assert init_body["result"]["protocolVersion"] == "2024-11-05"
    assert init_body["result"]["serverInfo"]["name"] == "adk-voice-agent-mcp"

    list_res = client.post(
        "/api/v1/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert list_res.status_code == 200
    tools = list_res.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    expected = {
        "list_threads",
        "read_thread",
        "update_user",
        "present_card",
        "recall",
        "recall_conversation",
        "search_web_grounded",
        "search_maps_grounded",
    }
    assert names == expected, (
        "save_to_drive is listed only with Drive connected"
    )
    assert len(tools) == len(MCP_TOOLS) - 1

    rest_list = client.get(
        "/api/v1/mcp/tools",
        headers={"X-Voice-Agent-Mcp-Token": token},
    )
    assert rest_list.status_code == 200
    assert rest_list.json()["user_id"] == "user@example.com"
    assert {t["name"] for t in rest_list.json()["tools"]} == names


def test_shared_thread_journal_list_and_read_thread(
    _clean_mcp_and_registry: ThreadRegistry,
) -> None:
    registry = _clean_mcp_and_registry
    _add_thread(
        registry,
        thread_id="thread-sibling1",
        owner="ada@example.com",
        title="Investigate Redis pool latency",
        state=ThreadState.COMPLETED,
        result_text="Recommended min_size=10 and max_size=50 in config/redis.yaml.",
    )
    _add_thread(
        registry,
        thread_id="thread-current2",
        owner="ada@example.com",
        title="Write Q3 latency report",
        state=ThreadState.EXECUTING,
        result_text="Drafting report.",
    )
    _add_thread(
        registry,
        thread_id="thread-otherusr",
        owner="someoneelse@example.com",
        title="Secret thread",
        state=ThreadState.COMPLETED,
    )

    token = mint_mcp_token("thread-current2", "ada@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    res = client.post(
        "/api/v1/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "list_threads",
                "arguments": {"query": "latency"},
            },
        },
    )
    assert res.status_code == 200
    payload = res.json()["result"]
    assert payload["isError"] is False
    listed = payload["structuredContent"]["threads"]
    assert [t["thread_id"] for t in listed] == [
        "thread-current2",
        "thread-sibling1",
    ]
    assert all(t["thread_id"] != "thread-otherusr" for t in listed)

    detail_res = client.post(
        "/api/v1/mcp/call",
        headers=headers,
        json={
            "tool": "read_thread",
            "arguments": {"thread_id": "thread-sibling1"},
        },
    )
    assert detail_res.status_code == 200
    detail = detail_res.json()["result"]
    assert detail["thread_id"] == "thread-sibling1"
    assert detail["title"] == "Investigate Redis pool latency"
    assert detail["state"] == "completed"
    assert "min_size=10" in detail["result_text"]
    assert detail["artifacts"][0]["title"] == "Design Notes"
    assert len(detail["events"]) == 1

    other_res = client.post(
        "/api/v1/mcp/call",
        headers=headers,
        json={
            "tool": "read_thread",
            "arguments": {"thread_id": "thread-otherusr"},
        },
    )
    assert other_res.status_code == 404


def test_memory_recall_over_mcp() -> None:
    episodes = FakeEpisodeStore()
    set_memory_store(FakeMemoryStore())
    set_episode_store(episodes)

    import asyncio

    asyncio.run(
        episodes.put(
            "local",
            "conversation",
            "ep-1",
            "Decided to use Redis Streams for event fanout.",
            {"session_id": "sess-1"},
        )
    )

    token = mint_mcp_token("thread-mem01", "local")
    headers = {"Authorization": f"Bearer {token}"}

    recall_res = client.post(
        "/api/v1/mcp/call",
        headers=headers,
        json={"tool": "recall", "arguments": {"query": "Redis Streams"}},
    )
    assert recall_res.status_code == 200
    assert "Redis Streams" in recall_res.json()["result"]
