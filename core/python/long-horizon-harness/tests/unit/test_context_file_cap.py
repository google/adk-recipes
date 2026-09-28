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

"""``discover_context_files`` caps any single file at ``MAX_CONTEXT_FILE_BYTES``.

A stray multi-MB CLAUDE.md paste should not blow the prefix budget — the
loader truncates with a ``[truncated]`` marker and keeps going.
"""

from __future__ import annotations

from pathlib import Path

from app.conversation.system_prompt import (
    MAX_CONTEXT_FILE_BYTES,
    discover_context_files,
)


def test_max_context_file_bytes_matches_lha_cap():
    # The harness caps each context source at 20K chars.
    assert MAX_CONTEXT_FILE_BYTES == 20_000


def test_small_file_passes_through_untouched(tmp_path: Path):
    body = "# small\n\nfits comfortably under the cap."
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")

    found = discover_context_files(tmp_path)
    assert found == [("AGENTS.md", body)]


def test_oversize_file_is_truncated_with_marker(tmp_path: Path):
    body = "x" * (MAX_CONTEXT_FILE_BYTES + 4096)
    (tmp_path / "CLAUDE.md").write_text(body, encoding="utf-8")

    found = discover_context_files(tmp_path)
    assert len(found) == 1
    name, text = found[0]
    assert name == "CLAUDE.md"
    encoded = text.encode("utf-8")
    assert len(encoded) <= MAX_CONTEXT_FILE_BYTES + len(b"\n\n[truncated]")
    assert text.endswith("[truncated]")


def test_cap_applies_to_each_context_filename(tmp_path: Path):
    """Even though discovery is first-match-wins (so only one file loads per
    cwd in practice), the cap behavior must hold for whichever file ends up
    winning. Exercise each priority slot in isolation."""
    body = "y" * (MAX_CONTEXT_FILE_BYTES * 2)
    for name in ("AGENTS.md", "CLAUDE.md", ".cursorrules"):
        per_file_dir = tmp_path / name.replace(".", "_")
        per_file_dir.mkdir()
        (per_file_dir / name).write_text(body, encoding="utf-8")

        found = discover_context_files(per_file_dir)
        assert len(found) == 1
        loaded_name, text = found[0]
        assert loaded_name == name
        assert text.endswith("[truncated]")
        assert len(text.encode("utf-8")) <= MAX_CONTEXT_FILE_BYTES + len(
            b"\n\n[truncated]"
        )


def test_empty_file_is_still_skipped(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("   \n  \t  ", encoding="utf-8")

    assert discover_context_files(tmp_path) == []


def test_unicode_truncation_does_not_raise(tmp_path: Path):
    # Build a body where the cap boundary lands mid-multibyte to ensure
    # the truncation uses errors="ignore" rather than raising.
    body = "é" * (MAX_CONTEXT_FILE_BYTES // 2 + 100)
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")

    found = discover_context_files(tmp_path)
    assert len(found) == 1
    _, text = found[0]
    assert text.endswith("[truncated]")
