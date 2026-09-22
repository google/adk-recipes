import asyncio
import time

import pytest

from app import mcp_server


@pytest.mark.asyncio
class TestAskKnowledgeBaseDoesNotBlockTheEventLoop:
    """The MCP server is mounted into the FastAPI app and shares its event
    loop, so the blocking Vector Search and Gemini calls must be offloaded.
    """

    async def test_other_tasks_progress_while_search_blocks(self, monkeypatch):
        block_for = 0.4

        def slow_search(**_kwargs):
            time.sleep(block_for)  # simulates the gRPC round trip
            return "## Context provided:\nsomething"

        monkeypatch.setattr(mcp_server, "search_knowledge_base", slow_search)
        monkeypatch.setattr(
            mcp_server, "generate_answer", lambda *_a, **_k: "answer"
        )

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            result = await mcp_server.ask_knowledge_base(
                conversation_summary="", question="q"
            )
        finally:
            tick_task.cancel()

        assert result == "answer"
        # If the sync call ran inline on the loop, ticks would be ~0.
        assert ticks > 5, (
            f"event loop appears blocked: only {ticks} ticks during "
            f"{block_for}s of synchronous work"
        )

    async def test_generation_is_also_offloaded(self, monkeypatch):
        block_for = 0.4

        monkeypatch.setattr(
            mcp_server,
            "search_knowledge_base",
            lambda **_k: "## Context provided:\nsomething",
        )

        def slow_generate(*_args, **_kwargs):
            time.sleep(block_for)
            return "answer"

        monkeypatch.setattr(mcp_server, "generate_answer", slow_generate)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        try:
            result = await mcp_server.ask_knowledge_base(
                conversation_summary="", question="q"
            )
        finally:
            tick_task.cancel()

        assert result == "answer"
        assert ticks > 5, (
            f"event loop appears blocked: only {ticks} ticks during "
            f"{block_for}s of synchronous work"
        )


@pytest.mark.asyncio
async def test_search_failure_returns_generic_message(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError(
            "projects/secret-proj/locations/us-central1/collections/x not found"
        )

    monkeypatch.setattr(mcp_server, "search_knowledge_base", boom)

    result = await mcp_server.ask_knowledge_base(
        conversation_summary="", question="q"
    )

    assert "secret-proj" not in result
    assert "RuntimeError" not in result
    assert "knowledge base search failed" in result.lower()


@pytest.mark.asyncio
async def test_generation_failure_returns_generic_message(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "search_knowledge_base",
        lambda **_k: "## Context provided:\nsomething",
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("backend detail projects/secret-proj/foo")

    monkeypatch.setattr(mcp_server, "generate_answer", boom)

    result = await mcp_server.ask_knowledge_base(
        conversation_summary="", question="q"
    )

    assert "secret-proj" not in result
    assert "RuntimeError" not in result
    assert "could not generate an answer" in result.lower()


@pytest.mark.asyncio
async def test_top_k_upper_bound_is_advertised_and_enforced():
    """top_k fans out into Vector Search and then into the Gemini prompt,
    and the MCP server is externally reachable, so the bound must be real
    and not merely documented.
    """
    from app.config import MAX_TOP_K

    tools = await mcp_server.server.list_tools()
    tool = next(t for t in tools if t.name == "ask_knowledge_base")
    schema = tool.inputSchema["properties"]["top_k"]

    assert schema["maximum"] == MAX_TOP_K
    assert schema["minimum"] == 1

    with pytest.raises(Exception) as excinfo:
        await mcp_server.server.call_tool(
            "ask_knowledge_base",
            {"conversation_summary": "", "question": "q", "top_k": 10**9},
        )
    assert "validation error" in str(excinfo.value).lower()
