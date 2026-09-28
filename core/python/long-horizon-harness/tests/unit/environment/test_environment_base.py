# Copyright 2026 Google LLC
from pathlib import Path

import pytest

from app.environment import Environment


class _Fake(Environment):
    @property
    def working_dir(self) -> Path:
        return Path("/workspace")

    async def execute(self, command, *, timeout=None):  # type: ignore[override]
        raise NotImplementedError

    async def read_file(self, path):
        raise NotImplementedError

    async def write_file(self, path, content):
        raise NotImplementedError

    async def list_directory(self, path, *, limit):
        return [], False

    async def delete_file(self, path, *, recursive=False):
        return None

    async def make_dir(self, path):
        return None

    async def download_zip(self, path):
        return b""

    async def upload_zip(self, path, data):
        return None

    async def spawn_process(self, command, *, cwd=None, env=None):
        raise NotImplementedError


def test_capability_defaults_are_safe():
    env = _Fake()
    assert env.on_host_fs is False
    assert env.cache_identity() == "/workspace"


def test_missing_abstract_method_cannot_instantiate():
    class Incomplete(Environment):
        @property
        def working_dir(self):
            return Path("/workspace")

        async def execute(self, command, *, timeout=None):
            raise NotImplementedError

        async def read_file(self, path):
            raise NotImplementedError

        async def write_file(self, path, content):
            raise NotImplementedError

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
