# Copyright 2026 Google LLC

import pytest
import pytest_asyncio

from app.environment import Environment, LocalEnvironment
from app.environment.process import ProcessHandle


@pytest_asyncio.fixture
async def env(tmp_path):
    e = LocalEnvironment(working_dir=tmp_path)
    await e.initialize()
    return e


def test_is_environment_and_on_host_fs(tmp_path):
    e = LocalEnvironment(working_dir=tmp_path)
    assert isinstance(e, Environment)
    assert e.on_host_fs is True


@pytest.mark.asyncio
async def test_write_read_roundtrip(env, tmp_path):
    await env.write_file(tmp_path / "a.txt", "hello")
    assert await env.read_file(tmp_path / "a.txt") == b"hello"


@pytest.mark.asyncio
async def test_make_dir_and_list_directory_raw(env, tmp_path):
    await env.make_dir(tmp_path / "sub")
    await env.write_file(tmp_path / "sub" / "x.txt", "y")
    await env.make_dir(
        tmp_path / "__pycache__"
    )  # junk NOT filtered at this layer
    entries, truncated = await env.list_directory(tmp_path, limit=100)
    names = {e["name"]: e["kind"] for e in entries}
    assert names["sub"] == "dir"
    assert names["__pycache__"] == "dir"
    assert truncated is False
    sub, _ = await env.list_directory(tmp_path / "sub", limit=100)
    assert sub[0]["name"] == "x.txt"
    assert sub[0]["kind"] == "file"
    assert sub[0]["size"] == 1


@pytest.mark.asyncio
async def test_list_directory_truncates(env, tmp_path):
    for i in range(5):
        await env.write_file(tmp_path / f"f{i}.txt", "x")
    entries, truncated = await env.list_directory(tmp_path, limit=3)
    assert len(entries) == 3
    assert truncated is True


@pytest.mark.asyncio
async def test_delete_file_and_recursive(env, tmp_path):
    await env.write_file(tmp_path / "f.txt", "x")
    await env.delete_file(tmp_path / "f.txt")
    with pytest.raises(FileNotFoundError):
        await env.read_file(tmp_path / "f.txt")
    await env.make_dir(tmp_path / "d")
    await env.write_file(tmp_path / "d" / "n.txt", "x")
    await env.delete_file(tmp_path / "d", recursive=True)
    assert not (tmp_path / "d").exists()


@pytest.mark.asyncio
async def test_zip_roundtrip(env, tmp_path):
    await env.make_dir(tmp_path / "src")
    await env.write_file(tmp_path / "src" / "a.txt", "AAA")
    data = await env.download_zip(tmp_path / "src")
    assert data[:2] == b"PK"
    await env.upload_zip(tmp_path / "dst", data)
    assert await env.read_file(tmp_path / "dst" / "a.txt") == b"AAA"


@pytest.mark.asyncio
async def test_spawn_process_runs(env):
    handle = await env.spawn_process("echo hi")
    assert isinstance(handle, ProcessHandle)
    code = await handle.wait(timeout=5)
    assert code == 0
    data, _, _ = await handle.read()
    assert b"hi" in data


@pytest.mark.asyncio
async def test_spawn_process_env_merges_over_os_environ(env):
    # Popen(env=...) REPLACES the child env; secrets must be merged over
    # os.environ so PATH/HOME survive alongside the injected secret.
    handle = await env.spawn_process(
        'printf "SECRET=%s HOME=[%s]" "$SECRET" "$HOME"',
        env={"SECRET": "sv"},
    )
    code = await handle.wait(timeout=5)
    data, _, _ = await handle.read()
    assert code == 0
    text = data.decode()
    assert "SECRET=sv" in text
    assert (
        "HOME=[]" not in text
    )  # HOME survived the merge (Popen env would drop it)
