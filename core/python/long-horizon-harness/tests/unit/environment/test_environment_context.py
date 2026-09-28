# Copyright 2026 Google LLC
import pytest

from app.environment import LocalEnvironment
from app.environment_context import (
    active_environment,
    clear_active_environment,
    set_active_environment,
)


def test_active_environment_roundtrip(tmp_path):
    env = LocalEnvironment(working_dir=tmp_path)
    set_active_environment(env)
    assert active_environment() is env
    clear_active_environment()
    with pytest.raises(RuntimeError):
        active_environment()


def test_session_start_builds_horizon_local(monkeypatch, tmp_path):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")
    monkeypatch.setenv("LHA_LOCAL_ROOT", str(tmp_path))
    from app.conversation import session_start

    env, pending = session_start._build_environment("alice")
    assert isinstance(env, LocalEnvironment)
    assert hasattr(env, "list_directory")
    assert pending is None
