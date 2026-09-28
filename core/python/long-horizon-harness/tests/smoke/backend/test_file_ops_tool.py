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

"""``write_file`` → ``read_file`` round-trip + path-safety guards."""

from __future__ import annotations

import pytest
from google.adk.environment import BaseEnvironment

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_write_then_read_round_trips(
    active_env: BaseEnvironment, unique_name: str
) -> None:
    from app.tools.file_ops import read_file, write_file

    payload = f"hello from {unique_name}\nsecond line\n"
    write_result = await write_file(path=f"{unique_name}.txt", content=payload)
    assert write_result["success"] is True

    read_result = await read_file(path=f"{unique_name}.txt")
    assert read_result["success"] is True
    assert f"1: hello from {unique_name}" in read_result["content"]
    assert "2: second line" in read_result["content"]


async def test_read_traversal_is_rejected(active_env: BaseEnvironment) -> None:
    from app.tools.file_ops import read_file

    result = await read_file(path="../../../etc/passwd")
    assert result["success"] is False
    assert "outside working_dir" in result["error"]


async def test_write_traversal_is_rejected(active_env: BaseEnvironment) -> None:
    from app.tools.file_ops import write_file

    result = await write_file(path="/tmp/escape.txt", content="x")
    assert result["success"] is False
    assert "outside working_dir" in result["error"]
