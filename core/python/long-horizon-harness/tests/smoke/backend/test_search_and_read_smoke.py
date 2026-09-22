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

"""``search_files`` (regex + mtime ranking) and ``read_file`` (line-numbered
output + trailer) route through the shared environment.

Direct tool-call rather than LLM-driven — same pattern as
``test_terminal_tool.py`` / ``test_file_ops_tool.py``. The smoke contract is
"tool dispatch + env wiring works against the real backend"; LLM behavior is
covered by ``tests/eval``. Files and mtimes are set through the env interface
(``write_file`` / ``terminal``) so the test exercises the sandbox path too.
"""

from __future__ import annotations

import pytest
from google.adk.environment import BaseEnvironment

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_search_files_regex_matches(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import search_files, write_file

    sub = unique_name
    await write_file(
        path=f"{sub}/hit.py", content="def needle_42():\n    pass\n"
    )
    await write_file(path=f"{sub}/miss.py", content="def other():\n    pass\n")

    result = await search_files(r"needle_\d+", path=sub)

    assert result["success"] is True
    matched_paths = {m["path"] for m in result["matches"]}
    assert any(p.endswith("hit.py") for p in matched_paths)
    assert not any(p.endswith("miss.py") for p in matched_paths)


async def test_search_files_ranks_recent_first(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import search_files, write_file
    from horizon.tools.processes.terminal import terminal

    sub = unique_name
    await write_file(path=f"{sub}/old.py", content="needle\n")
    await write_file(path=f"{sub}/new.py", content="needle\n")
    # Force a clear mtime ordering through the env interface: old is older.
    await terminal(command=f"touch -t 202001010000 {sub}/old.py")
    await terminal(command=f"touch -t 202401010000 {sub}/new.py")

    result = await search_files("needle", path=sub)

    assert result["success"] is True
    ordered_paths = [m["path"] for m in result["matches"]]
    assert ordered_paths[0].endswith("new.py")
    assert ordered_paths[-1].endswith("old.py")
    assert all("mtime" not in m for m in result["matches"])
    assert "truncated" in result


async def test_search_invalid_regex_returns_error(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import search_files, write_file

    sub = unique_name
    await write_file(path=f"{sub}/a.py", content="anything\n")

    result = await search_files("[unclosed", path=sub)

    assert result["success"] is False
    assert "error" in result
    assert (
        "regex" in result["error"].lower()
        or "pattern" in result["error"].lower()
    )


async def test_read_file_is_line_numbered_with_eof_trailer(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import read_file, write_file

    target = f"{unique_name}.txt"
    await write_file(path=target, content="alpha\nbeta\ngamma\n")

    result = await read_file(path=target)

    assert result["success"] is True
    assert "1: alpha" in result["content"]
    assert "2: beta" in result["content"]
    assert "3: gamma" in result["content"]
    assert "End of file - total 3 lines" in result["content"]


async def test_read_file_continuation_trailer_on_partial_read(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from horizon.tools.file_ops import read_file, write_file

    target = f"{unique_name}.txt"
    payload = "\n".join(f"l{i}" for i in range(1, 101)) + "\n"
    await write_file(path=target, content=payload)

    result = await read_file(path=target, offset=1, limit=10)

    assert result["success"] is True
    assert "1: l1" in result["content"]
    assert (
        "Showing lines 1-10 of 100. Use offset=11 to continue."
        in result["content"]
    )
