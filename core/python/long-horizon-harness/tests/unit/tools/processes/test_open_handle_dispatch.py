# Copyright 2026 Google LLC
import importlib
from pathlib import Path

import pytest

# import_module bypasses the package __init__'s `terminal` attribute (which is the
# terminal *function*, shadowing the submodule) and returns the real module.
terminal_mod = importlib.import_module("horizon.tools.processes.terminal")
# open_handle moved to _spawn.py when the parameter space shrank: bash's
# auto-promote-on-timeout path and process(action='spawn') both need the
# identical env.spawn_process + secret-injection call, so it now lives in
# one shared module instead of being duplicated or cross-imported.
spawn_mod = importlib.import_module("horizon.tools.processes._spawn")


class _RecordingEnv:
    on_host_fs = True

    def __init__(self):
        self.calls = []

    async def spawn_process(self, command, *, cwd=None, env=None):
        self.calls.append((command, cwd, env))
        return "HANDLE"


@pytest.mark.asyncio
async def test_open_handle_uses_env_spawn(monkeypatch):
    rec = _RecordingEnv()
    monkeypatch.setattr(spawn_mod, "active_environment", lambda: rec)

    async def _fake_secret_env():
        return {"K": "V"}

    monkeypatch.setattr("horizon.secrets.secret_env", _fake_secret_env)

    handle = await spawn_mod.open_handle("echo hi", Path("/workspace"))
    assert handle == "HANDLE"
    assert rec.calls == [("echo hi", Path("/workspace"), {"K": "V"})]


def test_no_isinstance_in_terminal():
    src = Path(terminal_mod.__file__).read_text()
    assert "isinstance(env, SandboxEnvironment)" not in src
