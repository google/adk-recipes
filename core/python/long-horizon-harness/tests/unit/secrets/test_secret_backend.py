# Copyright 2026 Google LLC
"""P1: SecretStore Protocol + LHA_SECRET_BACKEND selector + non-GCP fake."""

from __future__ import annotations

import sys

import pytest

from app.secrets import (
    InMemorySecretStore,
    SecretManagerStore,
    SecretStore,
    get_secret_store,
    set_secret_store,
)
from app.secrets.store import reset_secret_store


@pytest.fixture(autouse=True)
def _reset():
    set_secret_store(None)
    reset_secret_store()
    yield
    set_secret_store(None)
    reset_secret_store()


def test_both_impls_satisfy_protocol():
    assert isinstance(InMemorySecretStore(), SecretStore)
    assert isinstance(
        SecretManagerStore(client=object(), project_id="p"), SecretStore
    )


def test_memory_backend_selected_without_gcp(monkeypatch):
    monkeypatch.setenv("LHA_SECRET_BACKEND", "memory")
    # Poison the GCP import path: the memory branch must never touch it.
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)
    store = get_secret_store()
    assert isinstance(store, InMemorySecretStore)


def test_default_backend_is_secret_manager(monkeypatch):
    monkeypatch.setenv("LHA_SECRET_BACKEND", "secretmanager")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            captured["built"] = True

    import google.cloud.secretmanager as sm

    monkeypatch.setattr(sm, "SecretManagerServiceAsyncClient", _FakeClient)
    store = get_secret_store()
    assert isinstance(store, SecretManagerStore)
    assert captured["built"] is True


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("LHA_SECRET_BACKEND", "vault")
    with pytest.raises(ValueError, match="LHA_SECRET_BACKEND"):
        get_secret_store()


def test_set_secret_store_override_wins(monkeypatch):
    monkeypatch.setenv("LHA_SECRET_BACKEND", "memory")
    fake = InMemorySecretStore()
    set_secret_store(fake)
    assert get_secret_store() is fake


@pytest.mark.asyncio
async def test_inmemory_full_surface_roundtrip():
    s = InMemorySecretStore(now_ms=lambda: 1000)
    assert await s.get_all("alice@x") == {}
    await s.set_secret("alice@x", "OPENAI_API_KEY", "sk-1")
    assert await s.get_all("alice@x") == {"OPENAI_API_KEY": "sk-1"}
    assert await s.list_names("alice@x") == [
        {"name": "OPENAI_API_KEY", "created_at": 1000}
    ]
    await s.set_many("alice@x", {"A": "1", "B": "2"})
    assert await s.get_all("alice@x") == {
        "OPENAI_API_KEY": "sk-1",
        "A": "1",
        "B": "2",
    }
    assert await s.delete_secret("alice@x", "A") is True
    assert await s.delete_secret("alice@x", "NOPE") is False
    assert "A" not in await s.get_all("alice@x")
    s.invalidate("alice@x")  # no-op, must exist on the surface


@pytest.mark.asyncio
async def test_inmemory_isolates_users():
    s = InMemorySecretStore()
    await s.set_secret("alice@x", "K", "alice")
    await s.set_secret("bob@x", "K", "bob")
    assert await s.get_all("alice@x") == {"K": "alice"}
    assert await s.get_all("bob@x") == {"K": "bob"}
