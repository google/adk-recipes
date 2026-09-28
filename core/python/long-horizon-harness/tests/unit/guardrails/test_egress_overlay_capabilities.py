# Copyright 2026 Google LLC
from pathlib import Path

from app.guardrails import _overlay


class _Local:
    on_host_fs = True
    working_dir = Path("/tmp/ws")

    def cache_identity(self):
        return "/tmp/ws"


class _Sandbox:
    on_host_fs = False

    def cache_identity(self):
        return "sbx-1"


def test_is_local_env_uses_capability():
    assert _overlay.is_local_env(_Local()) is True
    assert _overlay.is_local_env(_Sandbox()) is False


def test_overlay_cache_key_uses_identity():
    assert _overlay.overlay_cache_key(_Sandbox()) == "sbx-1"
    assert _overlay.overlay_cache_key(_Local()) == "/tmp/ws"


def test_no_localenv_isinstance_in_guards():
    for mod in (_overlay,):
        assert (
            "isinstance(env, LocalEnvironment)"
            not in Path(mod.__file__).read_text()
        )
