# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Path scoping: file_ops tools resolve relative paths under
``active_environment().working_dir`` and reject absolute paths outside it.

The conftest autouse fixture binds ``LocalEnvironment(working_dir=tmp_path)``,
so a relative path lands inside ``tmp_path`` and an absolute path is OK iff
it's under ``tmp_path``. Paths that escape (e.g. ``/etc/passwd``, ``../x``)
are rejected with ``success=False``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.asyncio,
]
# ----------------------------------------------------------------------
# Relative paths resolve under env.working_dir (not process cwd)
# ----------------------------------------------------------------------


async def test_write_file_relative_path_resolves_under_env(
    tmp_path: Path,
) -> None:
    from horizon.tools.file_ops import write

    result = await write("reports/q3.md", "hello")
    assert result["success"], result
    assert (tmp_path / "reports" / "q3.md").read_text() == "hello"


async def test_read_file_relative_path_resolves_under_env(
    tmp_path: Path,
) -> None:
    from horizon.tools.file_ops import read_file

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "note.txt").write_text("ok")
    result = await read_file("data/note.txt")
    assert result["success"] and "1: ok" in result["content"]


async def test_patch_relative_path_resolves_under_env(tmp_path: Path) -> None:
    from horizon.tools.file_ops import edit

    (tmp_path / "x.md").write_text("draft v1")
    result = await edit("x.md", [{"oldText": "v1", "newText": "v2"}])
    assert result["success"], result
    assert (tmp_path / "x.md").read_text() == "draft v2"


async def test_search_files_relative_path_resolves_under_env(
    tmp_path: Path,
) -> None:
    from horizon.tools.file_ops import search_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("hello world")
    result = await search_files("hello", path="src")
    assert result["success"]
    assert len(result["matches"]) == 1


# ----------------------------------------------------------------------
# Absolute paths under env.working_dir are allowed
# ----------------------------------------------------------------------


async def test_write_file_absolute_under_env_allowed(tmp_path: Path) -> None:
    from horizon.tools.file_ops import write

    target = tmp_path / "inside.txt"
    result = await write(str(target), "ok")
    assert result["success"], result
    assert target.read_text() == "ok"


# ----------------------------------------------------------------------
# Absolute paths outside env.working_dir are rejected
# ----------------------------------------------------------------------


async def test_write_file_absolute_outside_env_rejected(tmp_path: Path) -> None:
    from horizon.tools.file_ops import write

    # /tmp is outside tmp_path-rooted env on every platform pytest runs on.
    result = await write("/tmp/should-not-land.txt", "nope")
    assert result["success"] is False
    assert (
        "outside" in result["error"].lower()
        or "denied" in result["error"].lower()
    )


async def test_read_file_absolute_outside_env_rejected(tmp_path: Path) -> None:
    from horizon.tools.file_ops import read_file

    result = await read_file("/etc/hosts")
    assert result["success"] is False
    assert (
        "outside" in result["error"].lower()
        or "denied" in result["error"].lower()
    )


async def test_patch_absolute_outside_env_rejected(tmp_path: Path) -> None:
    from horizon.tools.file_ops import edit

    result = await edit("/etc/hosts", [{"oldText": "x", "newText": "y"}])
    assert result["success"] is False
    assert (
        "outside" in result["error"].lower()
        or "denied" in result["error"].lower()
    )


# ----------------------------------------------------------------------
# Relative .. escapes are rejected
# ----------------------------------------------------------------------


async def test_write_file_dotdot_escape_rejected(tmp_path: Path) -> None:
    from horizon.tools.file_ops import write

    result = await write("../escape.txt", "nope")
    assert result["success"] is False
    assert (
        "outside" in result["error"].lower()
        or "denied" in result["error"].lower()
    )


async def test_read_file_dotdot_escape_rejected(tmp_path: Path) -> None:
    from horizon.tools.file_ops import read_file

    result = await read_file("../../etc/passwd")
    assert result["success"] is False
    assert (
        "outside" in result["error"].lower()
        or "denied" in result["error"].lower()
    )
