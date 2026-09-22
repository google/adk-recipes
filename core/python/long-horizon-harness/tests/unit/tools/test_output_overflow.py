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

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from horizon.environment import LocalEnvironment
from horizon.environment_context import (
    clear_active_environment,
    set_active_environment,
)
from horizon.tools._output_overflow import (
    OVERFLOW_SUBDIR,
    make_preview,
    overflow_to_file,
)


def test_under_both_caps_returns_verbatim() -> None:
    text = "line1\nline2\nline3\n"
    preview, truncated = make_preview(text, max_bytes=1024, max_lines=100)
    assert preview == text
    assert truncated is False


def test_over_byte_cap_keeps_head_and_tail_with_marker() -> None:
    lines = [f"line-{i:04d}" for i in range(500)]
    text = "\n".join(lines)
    preview, truncated = make_preview(text, max_bytes=200, max_lines=100)
    assert truncated is True
    assert preview.startswith("line-0000")
    assert preview.rstrip().endswith("line-0499")
    assert "omitted" in preview
    assert len(preview.encode("utf-8")) < len(text.encode("utf-8"))


def test_over_line_cap_only_still_truncates() -> None:
    text = "\n".join("x" for _ in range(1000))  # tiny bytes, many lines
    preview, truncated = make_preview(text, max_bytes=1_000_000, max_lines=50)
    assert truncated is True
    assert "omitted" in preview


def test_never_splits_a_multibyte_char() -> None:
    # A single oversized line of 4-byte emoji; the byte-cut fallback must
    # land on a char boundary so the result re-encodes cleanly.
    line = "\U0001f600" * 500  # 500 emoji, 4 bytes each = 2000 bytes
    preview, truncated = make_preview(line, max_bytes=101, max_lines=100)
    assert truncated is True
    # Must round-trip — a mid-char cut would raise / produce replacement chars.
    preview.encode("utf-8").decode("utf-8")
    assert "�" not in preview


def test_marker_reports_omitted_counts() -> None:
    lines = [f"line-{i:04d}" for i in range(500)]
    text = "\n".join(lines)
    preview, _ = make_preview(text, max_bytes=200, max_lines=100)

    m = re.search(r"(\d+) bytes / (\d+) lines omitted", preview)
    assert m is not None
    assert int(m.group(1)) > 0
    assert int(m.group(2)) > 0


@pytest.fixture
def env_root(tmp_path: Path) -> Iterator[Path]:
    working_dir = tmp_path / "ws"
    working_dir.mkdir()
    set_active_environment(LocalEnvironment(working_dir=working_dir))
    prev = os.getcwd()
    os.chdir(working_dir)
    try:
        yield working_dir
    finally:
        os.chdir(prev)
        clear_active_environment()


@pytest.mark.asyncio
async def test_overflow_writes_full_text_and_returns_pointer(
    env_root: Path,
) -> None:
    big = "\n".join(f"row-{i:05d}" for i in range(5000))
    result = await overflow_to_file(
        big, stream="stdout", id_factory=lambda: "deadbeef"
    )
    written = Path(result.path).read_bytes().decode("utf-8")
    assert written == big  # FULL text preserved, nothing dropped
    assert result.truncated is True
    assert "Full output saved to" in result.pointer
    assert "read with offset/limit" in result.pointer
    assert "delegate a subagent" in result.pointer
    assert result.preview.startswith("row-00000")


@pytest.mark.asyncio
async def test_overflow_id_is_injectable_and_used_in_filename(
    env_root: Path,
) -> None:
    big = "x\n" * 100_000
    result = await overflow_to_file(
        big, stream="stderr", id_factory=lambda: "fixedid01"
    )
    assert Path(result.path).name == "stderr-fixedid01.txt"
    # Resolved under the workspace root, not an absolute /workspace path.
    assert Path(result.path).parent == (env_root / OVERFLOW_SUBDIR).resolve()
    assert Path(result.path).is_relative_to(env_root)


@pytest.mark.asyncio
async def test_pointer_reports_line_and_byte_counts(env_root: Path) -> None:
    big = "\n".join("c" * 80 for _ in range(2000))
    result = await overflow_to_file(
        big, stream="stdout", id_factory=lambda: "z"
    )
    n_lines = big.count("\n") + 1
    n_bytes = len(big.encode("utf-8"))
    assert f"({n_lines} lines / {n_bytes} bytes)" in result.pointer
