# Copyright 2026 Google LLC
"""P2: the memory-backend adapter abstraction (factory dispatch + degradation)."""

from __future__ import annotations

import pytest
from google.adk.memory import InMemoryMemoryService

from app.memory.adapter import (
    InMemoryMemoryAdapter,
    MemoryAdapter,
    NoopMemoryAdapter,
    VertexMemoryAdapter,
    memory_adapter,
)


def test_factory_dispatch_in_memory():
    adapter = memory_adapter(InMemoryMemoryService())
    assert isinstance(adapter, InMemoryMemoryAdapter)
    assert isinstance(adapter, MemoryAdapter)
    assert adapter.supports_profiles() is False


def test_factory_dispatch_vertex():
    from google.adk.memory import VertexAiMemoryBankService

    service = VertexAiMemoryBankService(
        project="p", location="us-central1", agent_engine_id="123"
    )
    adapter = memory_adapter(service)
    assert isinstance(adapter, VertexMemoryAdapter)
    assert isinstance(adapter, MemoryAdapter)
    assert adapter.supports_profiles() is True


def test_factory_dispatch_unknown_service_is_noop():
    adapter = memory_adapter(object())
    assert isinstance(adapter, NoopMemoryAdapter)
    assert isinstance(adapter, MemoryAdapter)
    assert adapter.supports_profiles() is False


@pytest.mark.asyncio
async def test_noop_degrades_cleanly():
    adapter = memory_adapter(object())
    assert await adapter.retrieve_profile(app_name="a", user_id="u") is None
    assert await adapter.list_all(app_name="a", user_id="u") == []
    with pytest.raises(NotImplementedError):
        await adapter.generate_profile(
            app_name="a", user_id="u", events=[], consolidate=True
        )
