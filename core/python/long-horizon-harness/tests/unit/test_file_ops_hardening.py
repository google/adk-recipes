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

"""file_ops hardening pack — 8 safety/edge cases not covered by test_file_ops.

Pins safety gaps in ``horizon/tools/file_ops.py`` not covered by the base
test file (``test_file_ops.py``), which covers happy-path
read/write/edit/search, the write-side deny list, and a few
binary/encoding fallbacks. This pack adds the safety edges that path
doesn't reach.

Implementation notes for ``horizon/tools/file_ops.py`` (~30 LOC):

  A. Character-device/FIFO/socket blocking in ``read_file``:
       reading ``/dev/urandom`` via ``Path.read_text`` will hang
       indefinitely (the call blocks waiting for bytes that never EOF).
       Reading a FIFO/socket has the same failure mode. Add a guard
       that rejects any non-regular file early.

  B. ``_MAX_READ_CHARS = 200_000`` cap in ``read_file``: an
       unbounded read can blow the model's context window with a
       single tool call. Cap the returned ``content`` (after offset/
       limit selection) and surface the truncation in the result so
       the model knows to paginate.

  D. ``read_file`` deny-list gating: the existing
       ``_is_write_denied`` blocks WRITES to /etc/passwd, ~/.ssh/*,
       etc. ``read_file`` does NOT consult this list — so the model
       can read /etc/passwd and exfiltrate it. Either extend
       ``_is_write_denied`` to gate reads too, or add a parallel
       ``_is_read_denied`` covering at least the secrets-bearing
       paths (/etc/shadow, ~/.ssh/id_*, ~/.aws/credentials, .netrc,
       .pgpass, ~/.gnupg, ~/.docker/config.json).

  E. (Parity test only — no impl change expected.) ``read_file``
       already uses ``errors="replace"`` so non-UTF8 bytes don't
       raise. This pack pins that behavior so a future refactor that
       drops ``errors="replace"`` is caught.

  F. Symlink-into-denied-path block: a symlink at
       ``~/innocent.txt`` pointing to ``/etc/shadow`` must be
       blocked. ``_is_write_denied`` already calls ``os.path.realpath``,
       but no test pins the symlink path. This pack adds that test
       for writes AND extends the contract to reads (paired with D).

Three docs/parity tests in the same file pin invariants that are
easy to regress under refactor:

  G. The cap constant is exported at module surface as
       ``_MAX_READ_CHARS`` so the value is greppable and tunable in
       one place.

  H. ``read_file(offset=0)`` is treated the same as ``offset=1`` —
       pins the 1-indexed convention against a "fixed an off-by-one"
       regression. Parity with lha ``file_tools.py`` semantics.

  I. ``search_files`` does NOT follow symlinks out of its search
       root — pins that ``os.walk`` is called without
       ``followlinks=True``. Important because a symlink in the
       repo pointing to ``/`` would otherwise cause search to walk
       the entire filesystem.

The tests in this module are written so that they will FAIL on the
current impl and PASS after the ~30 LOC impl delta. Each test names
the gap in its docstring so a future reader knows what's being pinned.

Surface assumption: the implementation will live in
``horizon/tools/file_ops.py`` and expose either ``_MAX_READ_CHARS`` (for
test G) and either ``_is_read_denied`` (a new helper) or an extended
``_is_write_denied`` that also gates reads. The contracts pinned by
the tests are stable across either naming choice.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from horizon.tools.file_ops import read_file, search_files, write

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.asyncio,
]
# =============================================================================
# A. Character-device / FIFO / socket: read_file must not hang
# =============================================================================


def _tmp_is_writable() -> bool:
    # AF_UNIX sun_path caps at 104 chars, so this needs /tmp rather than
    # pytest's long tmp_path. A restricted sandbox can make /tmp unwritable.
    try:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hsprobe"):
            return True
    except OSError:
        return False


class TestReadFileBlocksNonRegularFiles:
    @pytest.mark.skipif(
        not os.path.exists("/dev/urandom"),
        reason="character device path /dev/urandom not present on this platform",
    )
    async def test_read_dev_urandom_returns_error_not_hang(self) -> None:
        """``/dev/urandom`` is a character device that never EOFs.
        ``Path.read_text`` against it would block indefinitely. read_file
        must reject non-regular files (devices, FIFOs, sockets) early."""
        result = await read_file("/dev/urandom")

        assert result["success"] is False, (
            "read_file must reject /dev/urandom — letting it through "
            "hangs the agent. Got: " + repr(result)
        )
        assert result.get("error")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX FIFO via os.mkfifo is not available on Windows",
    )
    async def test_read_fifo_returns_error_not_hang(
        self, tmp_path: Path
    ) -> None:
        """A FIFO blocks on read until a writer connects. Same failure mode
        as /dev/urandom — read_file must reject it up front."""
        fifo_path = tmp_path / "myfifo"
        os.mkfifo(str(fifo_path))

        result = await read_file(str(fifo_path))

        assert result["success"] is False, (
            "read_file must reject FIFOs — letting one through hangs the "
            "agent forever. Got: " + repr(result)
        )
        assert result.get("error")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX domain sockets via socket.AF_UNIX are not on Windows",
    )
    @pytest.mark.skipif(
        not _tmp_is_writable(), reason="/tmp is not writable in this sandbox"
    )
    async def test_read_unix_socket_returns_error_not_hang(self) -> None:
        """Reading a UNIX domain socket file via the filesystem path raises
        (or hangs depending on platform). Either way, read_file must reject
        it deterministically rather than relying on the OS to error.

        Uses a short /tmp path because pytest's tmp_path on macOS
        (/private/tmp/pytest-of-.../...) exceeds the 104-char sun_path limit."""
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="hs") as td:
            sock_path = Path(td) / "s"
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(str(sock_path))
                result = await read_file(str(sock_path))
            finally:
                sock.close()

        assert result["success"] is False, (
            "read_file must reject UNIX sockets. Got: " + repr(result)
        )


# =============================================================================
# B. _MAX_READ_CHARS cap — prevent context blowup from one tool call
# =============================================================================


class TestReadFileCapsContentLength:
    async def test_huge_file_is_truncated_at_max_read_chars(
        self, tmp_path: Path
    ) -> None:
        """A 500KB file selected entirely by offset/limit must be capped
        at _MAX_READ_CHARS, now aligned with bash's TERMINAL_OUTPUT_LIMIT
        (50KB) instead of the old 200_000. Without the cap, one tool call
        can flood the model's context window.

        The cap now uses the same spill-to-file contract as bash
        (overflow_to_file): content is a preview plus a pointer sentence,
        so total content length can exceed _MAX_READ_CHARS by the pointer's
        overhead (a few hundred chars) — bounded generously here rather
        than with a strict <=, since bash's own overflow contract makes
        the same tradeoff and has no stricter test either."""
        from horizon.tools.file_ops import _MAX_READ_CHARS

        # Write ~500KB of content, well over the 50KB cap.
        target = tmp_path / "big.txt"
        line = "a" * 999 + "\n"  # 1000 chars per line
        target.write_text(line * 500)  # 500_000 chars total
        assert target.stat().st_size > _MAX_READ_CHARS

        result = await read_file(str(target), offset=1, limit=10_000)

        assert result["success"] is True
        # Content must stay bounded — original was 500_000 chars, so even
        # with pointer overhead this must be nowhere close to that.
        assert len(result["content"]) <= _MAX_READ_CHARS + 500, (
            f"read_file returned {len(result['content'])} chars, "
            f"exceeds cap of {_MAX_READ_CHARS} plus pointer overhead."
        )
        # The result must surface that truncation happened — otherwise the
        # model has no way to know it's looking at a partial file and may
        # confidently summarize as if it saw everything.
        assert result.get("truncated") is True, (
            "Truncation must be surfaced via result['truncated']=True so "
            "the model knows to paginate. Got: "
            f"{ {k: v for k, v in result.items() if k != 'content'} }"
        )

    async def test_huge_file_spills_full_content_with_pointer(
        self, tmp_path: Path
    ) -> None:
        """Same spill-to-lha/tool-output/ mechanism as bash's overflow
        contract, but the label differs on purpose: read spills only the
        requested offset/limit page, not the whole file, so the pointer
        says "This page of output", not "Full output" (which would
        overclaim a 502-line page of a much longer file as everything)."""
        target = tmp_path / "big.txt"
        lines = [f"line-{i}".ljust(999) + "\n" for i in range(500)]
        target.write_text("".join(lines))

        result = await read_file(str(target), offset=1, limit=10_000)

        assert result["success"] is True
        assert result["truncated"] is True
        assert "This page of output saved to" in result["content"]
        assert "Full output" not in result["content"]
        assert "read with offset/limit" in result["content"]
        assert "overflow_path" in result, (
            "Spill path must be surfaced so the caller (or a test) can "
            "verify the full content landed on disk. Got: " + repr(result)
        )

        saved = Path(result["overflow_path"]).read_text(encoding="utf-8")
        assert "line-0" in saved
        assert "line-499" in saved

    async def test_small_file_is_not_marked_truncated(
        self, tmp_path: Path
    ) -> None:
        """The truncation flag is opt-in — small reads must not carry a
        false ``truncated: True`` that would prompt the model to paginate
        a file it already saw in full."""
        target = tmp_path / "small.txt"
        target.write_text("just three\nlittle\nlines\n")

        result = await read_file(str(target))

        assert result["success"] is True
        # Either omitted or explicitly False — anything but a True flag.
        assert result.get("truncated") is not True, (
            "Small files must not be marked truncated. Got: " + repr(result)
        )


# =============================================================================
# D. Path traversal: read_file must block /etc/passwd & friends
# =============================================================================


class TestReadFileDenyList:
    async def test_read_etc_passwd_is_denied(self) -> None:
        """Currently read_file does NOT consult the deny list — only
        write does. So the model can read /etc/passwd and exfiltrate
        it via the response. This test pins that read_file rejects the
        same protected paths write does.

        On Linux /etc/passwd exists; on macOS it also exists. If it's
        absent on the test host (uncommon), the test skips rather than
        false-passing."""
        if not os.path.exists("/etc/passwd"):
            pytest.skip("/etc/passwd not present on this platform")

        from horizon.environment import LocalEnvironment
        from horizon.environment_context import set_active_environment

        # Bind the env root to "/" so /etc/passwd is IN-root and the read reaches
        # the protected-paths deny-list — not the out-of-root check, whose message
        # ("...outside working_dir...") could match "denied"/"protected"
        # incidentally via the tmp-dir name and false-pass.
        set_active_environment(LocalEnvironment(working_dir=Path("/")))

        result = await read_file("/etc/passwd")

        assert result["success"] is False, repr(result)
        assert "protected paths list" in result["error"].lower(), (
            f"read_file must reject /etc/passwd via the deny-list, not an "
            f"incidental IO/out-of-root error. Got: {result['error']!r}"
        )

    async def test_read_traversal_into_etc_passwd_is_denied(
        self, tmp_path: Path
    ) -> None:
        """Path traversal via ``../../../../etc/passwd`` must resolve to
        the real path and be blocked. The deny check must use
        ``os.path.realpath`` so traversal cannot bypass it."""
        if not os.path.exists("/etc/passwd"):
            pytest.skip("/etc/passwd not present on this platform")

        # Build a traversal that always resolves to /etc/passwd regardless
        # of how deep tmp_path is.
        traversal = str(tmp_path) + "/" + "../" * 20 + "etc/passwd"

        result = await read_file(traversal)

        assert result["success"] is False, (
            "read_file must resolve ../../../../etc/passwd and block it. "
            "Got: " + repr(result)
        )


# =============================================================================
# E. Non-UTF8: must not raise, must replace bytes
# =============================================================================


class TestReadFileNonUtf8:
    async def test_non_utf8_bytes_are_replaced_not_raised(
        self, tmp_path: Path
    ) -> None:
        """A file with invalid UTF-8 byte sequences must not crash
        read_file — it must surface the file with the U+FFFD replacement
        character substituted in. Pins that ``errors='replace'`` stays
        on the read_text call (a future refactor that drops it would
        regress the agent on any binary-ish text mix)."""
        target = tmp_path / "mixed.txt"
        # Lead with valid ASCII, then a lone 0x80 continuation byte (always
        # invalid in UTF-8), then more ASCII. ``errors='replace'`` maps the
        # invalid byte to U+FFFD.
        target.write_bytes(b"hello \x80 world\n")

        result = await read_file(str(target))

        assert result["success"] is True, (
            "Non-UTF8 must not fail the read — got: " + repr(result)
        )
        assert "hello" in result["content"]
        assert "world" in result["content"]
        # The U+FFFD replacement character marks where the invalid byte was.
        assert "�" in result["content"], (
            "Invalid UTF-8 byte must be substituted with U+FFFD. Got "
            f"content: {result['content']!r}"
        )


# =============================================================================
# F. Symlink-into-denied-path
# =============================================================================


class TestSymlinkResolution:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlinks need elevated privileges on Windows",
    )
    async def test_write_through_symlink_into_etc_passwd_is_denied(
        self, tmp_path: Path
    ) -> None:
        """A symlink in a safe-looking location that points to /etc/passwd
        must be blocked by the deny list. ``_is_write_denied`` already
        calls ``os.path.realpath`` — this test pins that the symlink
        follow actually happens at the deny-check site.

        Without this test, a future refactor that switches realpath to
        abspath (which doesn't follow symlinks) would silently break the
        deny-list and let the agent write through to /etc/passwd."""
        if not os.path.exists("/etc/passwd"):
            pytest.skip("/etc/passwd not present on this platform")

        link = tmp_path / "innocent.txt"
        link.symlink_to("/etc/passwd")

        result = await write(str(link), "x")

        assert result["success"] is False, (
            "Symlink to /etc/passwd must be blocked. Got: " + repr(result)
        )
        # Blocked either by deny-list (if target sits inside env) or by
        # working_dir scoping (if Path.resolve() follows the symlink out).
        msg = result["error"].lower()
        assert any(token in msg for token in ("denied", "protected", "outside"))


# =============================================================================
# G. (Docs/parity) _MAX_READ_CHARS is exported at module surface
# =============================================================================


class TestModuleConstants:
    def test_max_read_chars_constant_is_exposed(self) -> None:
        """The cap value must be importable from ``horizon.tools.file_ops`` so
        it's greppable and tunable in one place. Pins the module surface
        — a refactor that inlines the constant into the function would
        regress this."""
        from horizon.tools import file_ops

        assert hasattr(file_ops, "_MAX_READ_CHARS"), (
            "Module must expose _MAX_READ_CHARS as a module-level "
            "constant. Got attributes: "
            f"{[a for a in dir(file_ops) if 'MAX' in a or 'READ' in a]}"
        )
        cap = file_ops._MAX_READ_CHARS
        assert isinstance(cap, int)
        # Sanity: must be at least 10K (a useful read) and at most 1M
        # (otherwise it defeats its own purpose as a context guard).
        assert 10_000 <= cap <= 1_000_000, (
            f"_MAX_READ_CHARS={cap} is outside the sensible range [10_000, 1_000_000]."
        )


# =============================================================================
# H. (Docs/parity) read_file offset semantics — 0 and 1 behave the same
# =============================================================================


class TestReadFileOffsetParity:
    async def test_offset_zero_equivalent_to_offset_one(
        self, tmp_path: Path
    ) -> None:
        """lha treats offset=1 as the first line (1-indexed). The
        current impl clamps ``max(offset - 1, 0)``, so offset=0 and
        offset=1 both start at line 0 of the underlying list. Pin this
        equivalence against a future "fixed an off-by-one" regression
        that would silently skip line 1 when offset=0 was passed."""
        target = tmp_path / "lines.txt"
        target.write_text("alpha\nbeta\ngamma\ndelta\n")

        r0 = await read_file(str(target), offset=0, limit=10)
        r1 = await read_file(str(target), offset=1, limit=10)

        assert r0["success"] is True
        assert r1["success"] is True
        assert r0["content"] == r1["content"], (
            "offset=0 and offset=1 must return the same content "
            f"(1-indexed convention). Got r0={r0['content']!r} "
            f"vs r1={r1['content']!r}"
        )
        # And both must include 'alpha' (line 1).
        assert "alpha" in r0["content"]


# =============================================================================
# I. (Docs/parity) search_files does NOT follow symlinks out of root
# =============================================================================


class TestSearchFilesNoSymlinkFollow:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlinks need elevated privileges on Windows",
    )
    async def test_search_does_not_traverse_symlink_outside_root(
        self, tmp_path: Path
    ) -> None:
        """A symlink in the search root pointing OUT of the root (e.g.
        to a sibling directory) must not be traversed. Pins that
        ``os.walk`` is called without ``followlinks=True``.

        Without this guard, a symlink to ``/`` in the repo would cause
        ``search_files`` to walk the entire filesystem on every query."""
        # Two sibling directories. We search 'inside' and place a symlink
        # there pointing at 'outside'. The unique marker only exists in
        # outside/ — if search follows the symlink, it will surface a hit.
        inside = tmp_path / "inside"
        outside = tmp_path / "outside"
        inside.mkdir()
        outside.mkdir()
        (outside / "secrets.txt").write_text(
            "UNIQUE_MARKER_SHOULD_NOT_BE_FOUND\n"
        )
        (inside / "link_to_outside").symlink_to(outside)

        result = await search_files(
            "UNIQUE_MARKER_SHOULD_NOT_BE_FOUND", path=str(inside)
        )

        assert result["success"] is True
        # Zero matches — the marker lives outside the search root and the
        # symlink must not be followed.
        assert result["matches"] == [], (
            "search_files followed a symlink out of its search root. "
            "Found: " + repr(result["matches"])
        )
