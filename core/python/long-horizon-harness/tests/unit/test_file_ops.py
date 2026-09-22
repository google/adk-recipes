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

"""Deterministic tests for the file_ops tool surface.

The tool surface is four separate functions over the local filesystem
(pathlib).

The four tools pinned here are:

* `read_file(path, *, offset=1, limit=500)`
* `write(path, content)`
* `edit(path, edits: list[{oldText, newText}])`, a minimal parameter space.
  Each item matches a unique, non-overlapping region of the file AS READ
  (never a progressively-updated copy); the whole batch applies atomically
  or the file is untouched. No `replace_all`/`start`/`end` — pass N items
  for N occurrences, and there is no span-offset targeting successor
  (accepted capability loss, in favour of a minimal shape).
* `search_files(pattern, *, path=".", file_glob=None, limit=50)`

Each returns a dict with `success: bool` and either result data or
`error: str`. Tests pass `tmp_path` as the working root.

This module is *the spec* for ``horizon/tools/file_ops.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.asyncio,
]
# =============================================================================
# read_file
# =============================================================================


class TestReadFile:
    async def test_reads_existing_file(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "hello.txt"
        target.write_text("line1\nline2\nline3\n")

        result = await read_file(str(target))

        assert result["success"] is True
        assert "line1" in result["content"]
        assert "line2" in result["content"]
        assert "line3" in result["content"]

    async def test_missing_file_returns_error(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        result = await read_file(str(tmp_path / "does_not_exist.txt"))

        assert result["success"] is False
        assert "error" in result
        assert result["error"]

    async def test_empty_file_returns_empty_content(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "empty.txt"
        target.write_text("")

        result = await read_file(str(target))

        assert result["success"] is True
        assert result["content"] == ""

    async def test_offset_starts_at_requested_line(
        self, tmp_path: Path
    ) -> None:
        """offset=1 is the first line (1-indexed, matching lha convention)."""
        from horizon.tools.file_ops import read_file

        target = tmp_path / "numbered.txt"
        target.write_text("\n".join(f"line_{i}" for i in range(1, 101)) + "\n")

        result = await read_file(str(target), offset=50, limit=3)

        assert result["success"] is True
        assert "line_50" in result["content"]
        assert "line_51" in result["content"]
        assert "line_52" in result["content"]
        assert "line_49" not in result["content"]
        assert "line_53" not in result["content"]

    async def test_limit_caps_returned_lines(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "long.txt"
        target.write_text("\n".join(f"l{i}" for i in range(1, 1001)) + "\n")

        result = await read_file(str(target), offset=1, limit=10)

        assert result["success"] is True
        numbered = [
            ln
            for ln in result["content"].splitlines()
            if ln.strip() and ln.split(":", 1)[0].strip().isdigit()
        ]
        assert len(numbered) == 10

    async def test_offset_past_end_returns_empty(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "short.txt"
        target.write_text("only\nthree\nlines\n")

        result = await read_file(str(target), offset=999, limit=10)

        assert result["success"] is True
        assert result["content"].strip() == ""

    async def test_binary_file_is_rejected(self, tmp_path: Path) -> None:
        """Binary files (by extension) should be refused with a clear error."""
        from horizon.tools.file_ops import read_file

        target = tmp_path / "image.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        result = await read_file(str(target))

        assert result["success"] is False
        assert "binary" in result["error"].lower()

    async def test_directory_path_returns_error(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        result = await read_file(str(tmp_path))

        assert result["success"] is False
        assert result["error"]


class TestReadFileNumbering:
    async def test_lines_are_prefixed_with_1_indexed_numbers(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "numbered.txt"
        target.write_text("alpha\nbeta\ngamma\n")

        result = await read_file(str(target))

        assert result["success"] is True
        assert "1: alpha" in result["content"]
        assert "2: beta" in result["content"]
        assert "3: gamma" in result["content"]

    async def test_line_numbers_respect_offset(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "numbered.txt"
        target.write_text("\n".join(f"line_{i}" for i in range(1, 101)) + "\n")

        result = await read_file(str(target), offset=50, limit=3)

        assert result["success"] is True
        assert "50: line_50" in result["content"]
        assert "51: line_51" in result["content"]
        assert "52: line_52" in result["content"]

    async def test_long_line_is_capped_with_suffix(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import MAX_LINE_LENGTH, read_file

        target = tmp_path / "wide.txt"
        target.write_text("x" * 5000 + "\n")

        result = await read_file(str(target))

        assert result["success"] is True
        first_line = result["content"].splitlines()[0]
        assert first_line.startswith("1: ")
        assert "... (line truncated)" in first_line
        body = first_line[len("1: ") : -len("... (line truncated)")]
        assert len(body) == MAX_LINE_LENGTH

    async def test_short_line_is_not_truncated(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "ok.txt"
        target.write_text("short\n")

        result = await read_file(str(target))

        assert result["success"] is True
        assert "... (line truncated)" not in result["content"]

    async def test_complete_read_ends_with_end_of_file_trailer(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "small.txt"
        target.write_text("a\nb\nc\n")

        result = await read_file(str(target))

        assert result["success"] is True
        assert "End of file - total 3 lines" in result["content"]

    async def test_partial_read_ends_with_continuation_trailer(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "big.txt"
        target.write_text("\n".join(f"l{i}" for i in range(1, 101)) + "\n")

        result = await read_file(str(target), offset=1, limit=10)

        assert result["success"] is True
        assert (
            "Showing lines 1-10 of 100. Use offset=11 to continue."
            in (result["content"])
        )

    async def test_continuation_trailer_respects_offset(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "big.txt"
        target.write_text("\n".join(f"l{i}" for i in range(1, 101)) + "\n")

        result = await read_file(str(target), offset=20, limit=10)

        assert result["success"] is True
        assert (
            "Showing lines 20-29 of 100. Use offset=30 to continue."
            in (result["content"])
        )

    async def test_window_ending_exactly_at_eof_uses_end_trailer(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file

        target = tmp_path / "exact.txt"
        target.write_text("\n".join(f"l{i}" for i in range(1, 11)) + "\n")

        result = await read_file(str(target), offset=6, limit=5)

        assert result["success"] is True
        assert "End of file - total 10 lines" in result["content"]
        assert "Use offset=" not in result["content"]


# =============================================================================
# write
# =============================================================================


class TestWriteFile:
    async def test_creates_new_file(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "new.txt"
        result = await write(str(target), "hello world")

        assert result["success"] is True
        assert target.read_text() == "hello world"

    async def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "existing.txt"
        target.write_text("original")

        result = await write(str(target), "replaced")

        assert result["success"] is True
        assert target.read_text() == "replaced"

    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Writing to a path with missing intermediate dirs should auto-create them."""
        from horizon.tools.file_ops import write

        target = tmp_path / "nested" / "deeper" / "file.txt"
        result = await write(str(target), "ok")

        assert result["success"] is True
        assert target.read_text() == "ok"

    async def test_empty_content_creates_empty_file(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import write

        target = tmp_path / "empty.txt"
        result = await write(str(target), "")

        assert result["success"] is True
        assert target.exists()
        assert target.read_text() == ""

    async def test_deny_list_blocks_ssh_authorized_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ported from horizon test_file_write_safety.TestStaticDenyList — never write SSH keys."""
        from horizon.tools.file_ops import write

        monkeypatch.setenv("HOME", str(tmp_path))
        fake_ssh = tmp_path / ".ssh"
        fake_ssh.mkdir()
        target = fake_ssh / "authorized_keys"

        result = await write(str(target), "ssh-rsa AAAA...")

        assert result["success"] is False
        assert (
            "denied" in result["error"].lower()
            or "refused" in result["error"].lower()
        )
        assert not target.exists()

    async def test_deny_list_blocks_ssh_private_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from horizon.tools.file_ops import write

        monkeypatch.setenv("HOME", str(tmp_path))
        fake_ssh = tmp_path / ".ssh"
        fake_ssh.mkdir()
        target = fake_ssh / "id_rsa"

        result = await write(str(target), "-----BEGIN PRIVATE KEY-----")

        assert result["success"] is False
        assert not target.exists()

    async def test_deny_list_blocks_netrc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from horizon.tools.file_ops import write

        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / ".netrc"

        result = await write(
            str(target), "machine github.com login me password secret"
        )

        assert result["success"] is False
        assert not target.exists()

    async def test_deny_list_blocks_aws_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from horizon.tools.file_ops import write

        monkeypatch.setenv("HOME", str(tmp_path))
        aws_dir = tmp_path / ".aws"
        aws_dir.mkdir()
        target = aws_dir / "credentials"

        result = await write(str(target), "[default]\naws_access_key_id=...")

        assert result["success"] is False
        assert not target.exists()

    async def test_temp_file_not_denied(self, tmp_path: Path) -> None:
        """Ported from horizon test_temp_file_not_denied_by_default — sanity check."""
        from horizon.tools.file_ops import write

        target = tmp_path / "scratch.txt"
        result = await write(str(target), "ok")

        assert result["success"] is True


# =============================================================================
# edit
# =============================================================================


def _edits(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"oldText": old, "newText": new} for old, new in pairs]


class TestPatch:
    async def test_replaces_unique_old_text(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text('def greet():\n    print("hello")\n')

        result = await edit(
            str(target), _edits(('print("hello")', 'print("hi")'))
        )

        assert result["success"] is True
        assert target.read_text() == 'def greet():\n    print("hi")\n'

    async def test_missing_old_text_fails_and_leaves_file(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = 'def greet():\n    print("hello")\n'
        target.write_text(original)

        result = await edit(
            str(target), _edits(("this text is nowhere in the file", "x"))
        )

        assert result["success"] is False
        assert result["error"]
        assert target.read_text() == original

    async def test_non_unique_old_text_fails(self, tmp_path: Path) -> None:
        """When oldText appears more than once, the ambiguity must be
        flagged so the caller picks a more specific anchor — there is no
        replace_all to swap every occurrence; pass one edit per occurrence
        instead (each still needs its own unique anchor)."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text("x = 1\nx = 1\n")

        result = await edit(str(target), _edits(("x = 1", "x = 2")))

        assert result["success"] is False
        assert (
            "multiple" in result["error"].lower()
            or "ambig" in result["error"].lower()
            or "unique" in result["error"].lower()
        )
        # File must remain untouched on ambiguous match
        assert target.read_text() == "x = 1\nx = 1\n"

    async def test_batch_applies_every_edit_atomically(
        self, tmp_path: Path
    ) -> None:
        """The replacement for replace_all: N edits in one call, each
        matching a distinct, unique, non-overlapping region."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text("x = 1\ny = 1\nz = 1\n")

        result = await edit(
            str(target),
            _edits(("x = 1", "x = 2"), ("y = 1", "y = 2"), ("z = 1", "z = 2")),
        )

        assert result["success"] is True
        assert result["replacements"] == 3
        assert target.read_text() == "x = 2\ny = 2\nz = 2\n"

    async def test_one_bad_edit_leaves_the_whole_batch_untouched(
        self, tmp_path: Path
    ) -> None:
        """Atomicity: two edits would succeed on their own, but the third's
        oldText is absent — the file must come back exactly as it was, not
        partially edited."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = "x = 1\ny = 1\nz = 1\n"
        target.write_text(original)

        result = await edit(
            str(target),
            _edits(
                ("x = 1", "x = 2"),
                ("y = 1", "y = 2"),
                ("nowhere in the file", "z = 2"),
            ),
        )

        assert result["success"] is False
        assert target.read_text() == original

    async def test_second_of_three_bad_edit_leaves_file_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """Same atomicity claim, mismatch in the middle rather than last —
        rules out an implementation that applies edits as it validates them
        instead of resolving the whole batch against the original text
        first."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = "x = 1\ny = 1\nz = 1\n"
        target.write_text(original)

        result = await edit(
            str(target),
            _edits(
                ("x = 1", "x = 2"),
                ("nowhere in the file", "y = 2"),
                ("z = 1", "z = 2"),
            ),
        )

        assert result["success"] is False
        assert target.read_text() == original

    async def test_overlapping_edits_are_refused(self, tmp_path: Path) -> None:
        """Two edits targeting the same or adjacent text must be merged into
        one edit instead — applying both would make the second's match
        offset (computed against the original content) land on text the
        first edit already replaced."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = "value = 100\n"
        target.write_text(original)

        result = await edit(
            str(target),
            _edits(("value = 100", "value = 200"), ("100", "999")),
        )

        assert result["success"] is False
        assert "overlap" in result["error"].lower()
        assert target.read_text() == original

    async def test_empty_edits_list_is_rejected(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = "value = 1\n"
        target.write_text(original)

        result = await edit(str(target), [])

        assert result["success"] is False
        assert target.read_text() == original

    async def test_patch_missing_file_fails(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import edit

        result = await edit(str(tmp_path / "nope.py"), _edits(("a", "b")))

        assert result["success"] is False
        assert result["error"]

    async def test_patch_preserves_pipe_in_unmodified_lines(
        self, tmp_path: Path
    ) -> None:
        """Ported from horizon test_preserves_non_prefix_pipe_characters_in_unmodified_lines.

        A file containing pipe characters (e.g. shell commands) in unrelated
        lines must come through verbatim after an edit on a different line.
        """
        from horizon.tools.file_ops import edit

        target = tmp_path / "sample.py"
        target.write_text(
            "def run():\n"
            '    cmd = "echo a | sed s/a/b/"\n'
            "    result = 1\n"
            "    return result\n"
        )

        result = await edit(
            str(target),
            _edits(("    return result\n", "    return result + 1\n")),
        )

        assert result["success"] is True
        assert target.read_text() == (
            "def run():\n"
            '    cmd = "echo a | sed s/a/b/"\n'
            "    result = 1\n"
            "    return result + 1\n"
        )

    async def test_patch_preserves_long_lines(self, tmp_path: Path) -> None:
        """Ported from horizon test_apply_update_preserves_long_lines.

        A line >2000 chars elsewhere in the file must NOT be truncated by edit.
        """
        from horizon.tools.file_ops import edit

        long_line = "x" * 3000
        target = tmp_path / "wide.py"
        target.write_text(f"def short_func():\n    return 1\n{long_line}\n")

        result = await edit(
            str(target), _edits(("    return 1\n", "    return 2\n"))
        )

        assert result["success"] is True
        new = target.read_text()
        assert long_line in new
        assert "return 2" in new

    async def test_patch_preserves_large_files(self, tmp_path: Path) -> None:
        """Ported from horizon test_apply_update_file_over_2000_lines.

        Editing one line in a 2500-line file must keep all 2500 lines.
        """
        from horizon.tools.file_ops import edit

        lines = [f"line_{i}" for i in range(1, 2501)]
        lines[2200] = "old_value"  # 1-indexed line 2201
        target = tmp_path / "big.py"
        target.write_text("\n".join(lines) + "\n")

        result = await edit(str(target), _edits(("old_value", "new_value")))

        assert result["success"] is True
        written = target.read_text()
        assert "new_value" in written
        assert "old_value" not in written
        assert len(written.splitlines()) == 2500

    async def test_patch_on_deny_listed_path_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write deny list must also block patches that modify protected files."""
        from horizon.tools.file_ops import edit

        monkeypatch.setenv("HOME", str(tmp_path))
        fake_ssh = tmp_path / ".ssh"
        fake_ssh.mkdir()
        target = fake_ssh / "authorized_keys"
        target.write_text("ssh-rsa OLDKEY user@host\n")
        original = target.read_text()

        result = await edit(str(target), _edits(("OLDKEY", "EVILKEY")))

        assert result["success"] is False
        assert target.read_text() == original

    async def test_patch_succeeds_with_drifted_indentation(
        self, tmp_path: Path
    ) -> None:
        """Model supplies oldText with the wrong indent; fuzzy match recovers."""
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text(
            "class C:\n        def m(self):\n            return 1\n"
        )

        result = await edit(
            str(target),
            _edits(
                ("def m(self):\n    return 1", "def m(self):\n    return 2")
            ),
        )

        assert result["success"] is True
        assert "return 2" in target.read_text()
        assert "return 1" not in target.read_text()

    async def test_patch_succeeds_with_collapsed_whitespace(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text("x   =        1\n")

        result = await edit(str(target), _edits(("x = 1", "x = 2")))

        assert result["success"] is True
        assert target.read_text() == "x = 2\n"

    async def test_patch_block_anchor_when_interior_drifts(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        target.write_text("def f():\n    a = 1\n    b = 2\n    return a + b\n")

        # interior 'a = 1' line is wrong in oldText; anchors still match
        result = await edit(
            str(target),
            _edits(
                (
                    "def f():\n    a = 99\n    return a + b",
                    "def f():\n    return 0",
                )
            ),
        )

        assert result["success"] is True
        assert "return 0" in target.read_text()

    async def test_fuzzy_no_match_leaves_file_and_lists_strategies(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import edit

        target = tmp_path / "code.py"
        original = "value = 1\n"
        target.write_text(original)

        result = await edit(str(target), _edits(("totally absent text", "x")))

        assert result["success"] is False
        assert result["error"]
        assert target.read_text() == original

    async def test_edit_first_action_seeds_workspace_window(
        self, tmp_path: Path
    ) -> None:
        """First edit into a subdirectory must seed the workspace window."""
        from types import SimpleNamespace

        from horizon.tools.file_ops import edit
        from horizon.workspace_window import WORKSPACE_WINDOW_STATE_KEY

        sub = tmp_path.resolve() / "pkg"
        sub.mkdir()
        target = sub / "app.py"
        target.write_text("old = 1\n")

        ctx = SimpleNamespace(state={})
        result = await edit(
            str(target),
            _edits(("old = 1", "new = 2")),
            tool_context=ctx,
        )

        assert result["success"] is True
        assert ctx.state.get(WORKSPACE_WINDOW_STATE_KEY) == ["pkg"]


# =============================================================================
# search_files
# =============================================================================


class TestSearchFiles:
    async def test_finds_text_match_in_file(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("def needle():\n    pass\n")
        (tmp_path / "b.py").write_text("def other():\n    pass\n")

        result = await search_files("needle", path=str(tmp_path))

        assert result["success"] is True
        matched_paths = {m["path"] for m in result["matches"]}
        assert any(p.endswith("a.py") for p in matched_paths)
        assert not any(p.endswith("b.py") for p in matched_paths)

    async def test_no_matches_returns_empty_list(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("def foo(): pass\n")

        result = await search_files("zzzzzzz_not_here", path=str(tmp_path))

        assert result["success"] is True
        assert result["matches"] == []

    async def test_finds_match_in_nested_directory(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import search_files

        nested = tmp_path / "pkg" / "sub"
        nested.mkdir(parents=True)
        (nested / "deep.py").write_text("def needle():\n    pass\n")

        result = await search_files("needle", path=str(tmp_path))

        assert result["success"] is True
        matched_paths = {m["path"] for m in result["matches"]}
        assert any("deep.py" in p for p in matched_paths)

    async def test_file_glob_filters_extension(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("needle\n")
        (tmp_path / "b.txt").write_text("needle\n")

        result = await search_files(
            "needle", path=str(tmp_path), file_glob="*.py"
        )

        assert result["success"] is True
        matched_paths = {m["path"] for m in result["matches"]}
        assert any(p.endswith("a.py") for p in matched_paths)
        assert not any(p.endswith("b.txt") for p in matched_paths)

    async def test_limit_caps_result_count(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        for i in range(20):
            (tmp_path / f"f{i}.py").write_text("needle\n")

        result = await search_files("needle", path=str(tmp_path), limit=5)

        assert result["success"] is True
        assert len(result["matches"]) <= 5

    async def test_missing_path_returns_error(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        result = await search_files(
            "anything", path=str(tmp_path / "nonexistent_dir")
        )

        assert result["success"] is False
        assert result["error"]

    async def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        """Standard convention: ripgrep-style search skips dot-directories.

        Ported from horizon test_search_files_fallback_hidden_paths.
        """
        from horizon.tools.file_ops import search_files

        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config").write_text("needle\n")
        (tmp_path / "visible.py").write_text("needle\n")

        result = await search_files("needle", path=str(tmp_path))

        assert result["success"] is True
        matched_paths = {m["path"] for m in result["matches"]}
        assert any(p.endswith("visible.py") for p in matched_paths)
        assert not any(".git" in p for p in matched_paths)


class TestSearchFilesRegex:
    async def test_regex_pattern_matches(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("def needle_42():\n    pass\n")
        (tmp_path / "b.py").write_text("def other():\n    pass\n")

        result = await search_files(r"needle_\d+", path=str(tmp_path))

        assert result["success"] is True
        matched_paths = {m["path"] for m in result["matches"]}
        assert any(p.endswith("a.py") for p in matched_paths)
        assert not any(p.endswith("b.py") for p in matched_paths)

    async def test_anchored_regex(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("import os\nx = import_helper()\n")

        result = await search_files(r"^import ", path=str(tmp_path))

        assert result["success"] is True
        lines = {m["line"] for m in result["matches"]}
        assert lines == {1}

    async def test_invalid_regex_returns_error_not_crash(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("anything\n")

        result = await search_files("[unclosed", path=str(tmp_path))

        assert result["success"] is False
        assert "error" in result
        assert result["error"]
        assert "regex" in result["error"].lower() or "pattern" in (
            result["error"].lower()
        )

    async def test_matches_do_not_leak_mtime(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("needle\n")
        result = await search_files("needle", path=str(tmp_path))

        assert result["success"] is True
        assert result["matches"]
        assert all("mtime" not in m for m in result["matches"])
        assert result["truncated"] is False

    async def test_results_sorted_by_mtime_descending(
        self, tmp_path: Path
    ) -> None:
        import os
        import time

        from horizon.tools.file_ops import search_files

        old = tmp_path / "old.py"
        new = tmp_path / "new.py"
        old.write_text("needle\n")
        new.write_text("needle\n")
        now = time.time()
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now, now))

        result = await search_files("needle", path=str(tmp_path))

        assert result["success"] is True
        ordered_paths = [m["path"] for m in result["matches"]]
        assert ordered_paths[0].endswith("new.py")
        assert ordered_paths[-1].endswith("old.py")

    async def test_ignore_case_search(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        (tmp_path / "a.py").write_text("def NeedleCase(): pass\n")

        res_case = await search_files("needlecase", path=str(tmp_path))
        assert res_case["matches"] == []

        res_ic = await search_files(
            "needlecase", path=str(tmp_path), ignore_case=True
        )
        assert len(res_ic["matches"]) == 1
        assert res_ic["matches"][0]["line"] == 1

    async def test_excludes_noise_directories_by_default(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import search_files

        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("target_token\n")

        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("target_token\n")

        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "app.cpython.py").write_text("target_token\n")

        result = await search_files("target_token", path=str(tmp_path))
        assert result["success"] is True
        paths = [m["path"] for m in result["matches"]]
        assert len(paths) == 1
        assert "src/app.py" in paths[0] or "src" in paths[0]

    async def test_no_ignore_includes_noise_directories(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import search_files

        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("target_token\n")

        result = await search_files(
            "target_token", path=str(tmp_path), no_ignore=True
        )
        assert result["success"] is True
        assert len(result["matches"]) == 1
        assert "node_modules" in result["matches"][0]["path"]

    async def test_truncates_long_matched_lines(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import search_files

        prefix = "x" * 200
        suffix = "y" * 200
        (tmp_path / "bundle.js").write_text(f"{prefix}TARGET{suffix}\n")

        result = await search_files("TARGET", path=str(tmp_path))
        assert result["success"] is True
        assert len(result["matches"]) == 1
        text = result["matches"][0]["text"]
        assert "TARGET" in text
        assert len(text) <= 310
        assert text.startswith("...")
        assert text.endswith("...")

    async def test_skips_files_exceeding_max_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from horizon.tools import file_ops
        from horizon.tools.file_ops import search_files

        monkeypatch.setattr(file_ops, "_SEARCH_MAX_FILE_SIZE", 100)

        small = tmp_path / "small.txt"
        small.write_text("needle small\n")

        large = tmp_path / "large.txt"
        large.write_text("needle " + ("z" * 200) + "\n")

        result = await search_files("needle", path=str(tmp_path))
        assert result["success"] is True
        paths = [m["path"] for m in result["matches"]]
        assert len(paths) == 1
        assert "small.txt" in paths[0]


# =============================================================================
# Cross-cutting: round-trips and module surface
# =============================================================================


class TestModuleSurface:
    """Pin the public API surface."""

    def test_exports_four_tools(self) -> None:
        from horizon.tools import file_ops

        for name in ("read_file", "write", "edit", "search_files"):
            assert hasattr(file_ops, name), (
                f"horizon.tools.file_ops must export {name}"
            )
            assert callable(getattr(file_ops, name))


class TestRoundTrips:
    async def test_write_then_read_returns_same_content(
        self, tmp_path: Path
    ) -> None:
        from horizon.tools.file_ops import read_file, write

        target = tmp_path / "roundtrip.txt"
        payload = "alpha\nbeta\ngamma\n"

        write_result = await write(str(target), payload)
        assert write_result["success"] is True

        read_result = await read_file(str(target))
        assert read_result["success"] is True
        assert "1: alpha" in read_result["content"]
        assert "beta" in read_result["content"]
        assert "gamma" in read_result["content"]

    async def test_write_then_patch_then_read(self, tmp_path: Path) -> None:
        from horizon.tools.file_ops import edit, read_file, write

        target = tmp_path / "cycle.py"
        await write(str(target), "value = 1\n")

        patch_result = await edit(
            str(target), [{"oldText": "value = 1", "newText": "value = 42"}]
        )
        assert patch_result["success"] is True

        read_result = await read_file(str(target))
        assert "value = 42" in read_result["content"]


# Span-offset targeting (the former start/end kwargs) has no successor:
# minimizing the parameter space dropped it along with replace_all/
# old_string/new_string. Accepted capability loss: a highlighted-text edit on
# repeated content now needs a more specific oldText anchor instead.
