# Copyright 2026 Google LLC
from pathlib import Path

import pytest
import pytest_asyncio

from app.api import uploads
from app.environment import LocalEnvironment


@pytest_asyncio.fixture
async def env(tmp_path):
    e = LocalEnvironment(working_dir=tmp_path)
    await e.initialize()
    return e


@pytest.mark.asyncio
async def test_list_directory_filters_junk(env, tmp_path):
    await env.make_dir(tmp_path / "__pycache__")
    await env.write_file(tmp_path / "keep.txt", "x")
    entries, _ = await uploads._list_directory(env, tmp_path, limit=100)
    names = {e["name"] for e in entries}
    assert "keep.txt" in names
    assert "__pycache__" not in names


@pytest.mark.asyncio
async def test_delete_and_zip_via_env(env, tmp_path):
    await env.make_dir(tmp_path / "d")
    await env.write_file(tmp_path / "d" / "a.txt", "A")
    data = await uploads._download_dir_zip(env, tmp_path / "d")
    assert data[:2] == b"PK"
    await uploads._delete_path(env, tmp_path / "d" / "a.txt", recursive=False)
    assert not (tmp_path / "d" / "a.txt").exists()


def test_no_sandbox_isinstance_in_uploads():
    src = Path(uploads.__file__).read_text()
    assert "isinstance(env, SandboxEnvironment)" not in src
