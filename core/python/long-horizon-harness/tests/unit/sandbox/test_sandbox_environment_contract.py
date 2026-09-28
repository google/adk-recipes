# Copyright 2026 Google LLC
import pytest

from app.environment import Environment
from app.environment.sandbox import SandboxEnvironment


def _make(**over):
    kwargs = {
        "client": object(),
        "sandbox_name": "projects/p/locations/l/sandboxes/s1",
        "lb_host": "lb.example",
        "routing_token": "rt",
        "sandbox_token": "jwt",
        "owner": "alice",
    }
    kwargs.update(over)
    return SandboxEnvironment(**kwargs)


def test_is_environment_subclass():
    assert issubclass(SandboxEnvironment, Environment)


@pytest.mark.asyncio
async def test_capability_flags_and_cache_identity():
    env = _make()
    try:
        assert env.on_host_fs is False
        assert env.cache_identity() == "projects/p/locations/l/sandboxes/s1"
    finally:
        await env.close()
