# Copyright 2026 Google LLC
from pathlib import Path

import pytest

from app.conversation import session_start
from app.environment import LocalEnvironment
from app.environment_context import set_sandbox_provider
from app.sandbox.provider import (
    LocalProvider,
    SandboxProvider,
    UpgradeProvision,
    VertexSandboxProvider,
)


class _FakeProvider:
    """A non-Vertex provider that drives session_start's orchestration."""

    def __init__(self, env):
        self._env = env

    def build_environment(self, user_id):
        return self._env, None

    def build_routine_environment(self, run):
        return self._env, None

    def provision_upgrade(self, user_id):
        return UpgradeProvision(status="unavailable", reason="fake")

    def snapshot_and_prune(self, user_id):
        return {"status": "unavailable"}

    def provisioning_status(self, user_id):
        return {"status": "ready", "sandbox_name": "fake-sbx"}

    def will_provision(self):
        return True


def test_builtin_providers_satisfy_protocol():
    assert isinstance(LocalProvider(), SandboxProvider)
    assert isinstance(VertexSandboxProvider(), SandboxProvider)


@pytest.mark.asyncio
async def test_local_provider_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    p = LocalProvider()
    env, pending = p.build_environment("alice")
    assert isinstance(env, LocalEnvironment)
    assert pending is None
    assert p.will_provision() is False
    assert p.provisioning_status("alice") is None
    assert p.provision_upgrade("alice").status == "unavailable"
    assert p.snapshot_and_prune("alice") == {"status": "unavailable"}


class _CountingEnv(LocalEnvironment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.refresh_calls = 0

    async def refresh_auth(self) -> bool:
        self.refresh_calls += 1
        return True


@pytest.mark.asyncio
async def test_ensure_environment_refreshes_env_on_cache_hit(tmp_path):
    env = _CountingEnv(working_dir=tmp_path)
    set_sandbox_provider(_FakeProvider(env))
    session_start._env_cache.clear()
    try:
        assert await session_start._ensure_environment("alice") is env
        assert env.is_initialized
        # Cache hit dispatches to env.refresh_auth() polymorphically (no provider).
        assert await session_start._ensure_environment("alice") is env
        assert env.refresh_calls >= 1
    finally:
        set_sandbox_provider(None)
        session_start._env_cache.clear()


@pytest.mark.asyncio
async def test_upgrade_delegates_to_provider(tmp_path):
    fake = _FakeProvider(LocalEnvironment(working_dir=tmp_path))
    set_sandbox_provider(fake)
    try:
        result = await session_start.upgrade_user_sandbox("alice")
        assert result["status"] == "unavailable"
        assert result["reason"] == "fake"
    finally:
        set_sandbox_provider(None)


def test_provisioning_status_delegates(tmp_path):
    fake = _FakeProvider(LocalEnvironment(working_dir=tmp_path))
    set_sandbox_provider(fake)
    try:
        assert session_start.get_provisioning_status("alice") == {
            "status": "ready",
            "sandbox_name": "fake-sbx",
        }
    finally:
        set_sandbox_provider(None)


def test_no_sandbox_isinstance_in_session_start():
    # The last concrete-class isinstance (_refresh_sandbox_auth) moved to the
    # provider; session_start is now backend-agnostic.
    src = Path(session_start.__file__).read_text()
    assert "isinstance(env, SandboxEnvironment)" not in src
