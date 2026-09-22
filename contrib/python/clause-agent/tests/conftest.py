"""Shared pytest fixtures: isolate every test's file-backed state."""

from __future__ import annotations

import pytest

from clause_agent.tools import document_search, memory_bank


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Points every file-backed path at a fresh tmp_path per test.

    Also resets the memory_bank backend and clears document_search's corpus
    cache, so tests never leak state into each other or into the real POC
    data files.
    """
    monkeypatch.setenv(
        "CLAUSE_AGENT_MEMORY_PATH", str(tmp_path / "memory.json")
    )
    monkeypatch.setenv(
        "CLAUSE_AGENT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv(
        "CLAUSE_AGENT_LEGAL_QUEUE_PATH", str(tmp_path / "legal_queue.json")
    )
    memory_bank.set_backend(memory_bank.LocalJsonMemoryBankBackend())
    document_search._clear_corpus_cache()
    yield
    document_search._clear_corpus_cache()
