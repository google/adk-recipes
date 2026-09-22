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

"""Overflow-to-file for oversized tool output.

When a tool produces more than the inline cap, the full text is written to
``<workspace>/lha/tool-output/<id>.txt`` (snapshot-persistent in the sandbox)
and the tool returns a head+tail preview plus a pointer telling the model how
to read the rest. ``make_preview`` is the pure, IO-free trimmer; tests pin its
behavior deterministically.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from horizon.environment_context import active_environment
from horizon.tools._paths import path_under_root

# 50 KB per stream — large enough for a chatty build / test run, small enough
# that one command can't swamp a context already carrying skills + memory.
TERMINAL_OUTPUT_LIMIT = 50 * 1024
TERMINAL_OUTPUT_MAX_LINES = 800

# Workspace-root-relative — the single visible lha namespace (lha/todos/, etc.).
OVERFLOW_SUBDIR = "lha/tool-output"

_MARKER = "\n... [{bytes} bytes / {lines} lines omitted] ...\n"


def _overflow_dir() -> Path:
    """Resolve ``<workspace>/lha/tool-output`` for the active environment."""
    root = active_environment().working_dir.resolve()
    resolved, err = path_under_root(OVERFLOW_SUBDIR, root)
    if err is not None or resolved is None:
        raise RuntimeError(f"overflow dir escapes workspace root: {err}")
    return resolved


def _utf8_safe_tail(raw: bytes, max_bytes: int) -> str:
    """Last ``max_bytes`` of ``raw`` decoded on a char boundary."""
    start = max(0, len(raw) - max_bytes)
    # Walk forward off any UTF-8 continuation byte (0b10xxxxxx).
    while start < len(raw) and (raw[start] & 0xC0) == 0x80:
        start += 1
    return raw[start:].decode("utf-8", errors="replace")


def _utf8_safe_head(raw: bytes, max_bytes: int) -> str:
    """First ``max_bytes`` of ``raw`` decoded on a char boundary."""
    end = min(len(raw), max_bytes)
    # Walk back off any UTF-8 continuation byte (0b10xxxxxx) at the cut point.
    while 0 < end < len(raw) and (raw[end] & 0xC0) == 0x80:
        end -= 1
    return raw[:end].decode("utf-8", errors="replace")


def make_preview(
    text: str,
    *,
    max_bytes: int = TERMINAL_OUTPUT_LIMIT,
    max_lines: int = TERMINAL_OUTPUT_MAX_LINES,
) -> tuple[str, bool]:
    """Return ``(preview, truncated)``.

    Verbatim when under both caps. Otherwise head+tail of whole lines with a
    marker; a single oversized line falls back to a UTF-8-safe byte cut.
    """
    raw = text.encode("utf-8")
    lines = text.split("\n")
    if len(raw) <= max_bytes and len(lines) <= max_lines:
        return text, False

    budget = max_bytes - len(_MARKER) - 32  # leave room for the filled marker
    if budget <= 0:
        return _utf8_safe_head(raw, max_bytes), True

    head_budget = budget // 2
    tail_budget = budget - head_budget

    head: list[str] = []
    head_bytes = 0
    for i, line in enumerate(lines):
        size = len(line.encode("utf-8")) + (1 if i > 0 else 0)
        if head_bytes + size > head_budget or len(head) >= max_lines // 2:
            break
        head.append(line)
        head_bytes += size

    tail: list[str] = []
    tail_bytes = 0
    for line in reversed(lines):
        size = len(line.encode("utf-8")) + (1 if tail else 0)
        if tail_bytes + size > tail_budget or len(tail) >= max_lines // 2:
            break
        tail.insert(0, line)
        tail_bytes += size

    head_text = "\n".join(head)
    tail_text = "\n".join(tail)

    # Single line too big for either half — byte-cut it safely.
    if not head_text and not tail_text:
        head_text = _utf8_safe_head(raw, head_budget)
        tail_text = _utf8_safe_tail(raw, tail_budget)

    kept_bytes = len(head_text.encode("utf-8")) + len(tail_text.encode("utf-8"))
    kept_lines = len(head) + len(tail)
    marker = _MARKER.format(
        bytes=max(0, len(raw) - kept_bytes),
        lines=max(0, len(lines) - kept_lines),
    )
    return head_text + marker + tail_text, True


@dataclass(frozen=True)
class OverflowResult:
    preview: str
    pointer: str
    path: str
    truncated: bool


def _default_id() -> str:
    return uuid.uuid4().hex[:12]


async def overflow_to_file(
    text: str,
    *,
    stream: str,
    id_factory: Callable[[], str] = _default_id,
    max_bytes: int = TERMINAL_OUTPUT_LIMIT,
    max_lines: int = TERMINAL_OUTPUT_MAX_LINES,
    label: str = "Full output",
) -> OverflowResult:
    """Spill ``text`` to a snapshot-persistent file and build a pointer.

    Writes the FULL text via the active environment so the file rides the
    sandbox snapshot, then returns a head+tail preview plus a pointer telling
    the model how to read the rest. ``id_factory`` is injectable so tests pin
    the filename deterministically. ``label`` prefixes the pointer: bash's
    ``text`` really is the complete captured stream ("Full output"), but
    read spills only the requested offset/limit page, not the whole file, so
    it passes "This page of output" instead — "full" would overclaim a
    502-line page of a 5,000-line file as the entire thing.
    """
    preview, truncated = make_preview(
        text, max_bytes=max_bytes, max_lines=max_lines
    )
    path = _overflow_dir() / f"{stream}-{id_factory()}.txt"
    await active_environment().write_file(path, text)

    n_lines = text.count("\n") + 1
    n_bytes = len(text.encode("utf-8"))
    pointer = (
        f"{label} saved to {path} ({n_lines} lines / {n_bytes} bytes). "
        "Use read with offset/limit to read it, grep it via bash, "
        "or delegate a subagent to process it (don't read the whole thing "
        "yourself)."
    )
    return OverflowResult(
        preview=preview, pointer=pointer, path=str(path), truncated=truncated
    )


async def emit_stream(
    text: str, *, stream: str
) -> tuple[str, bool, str | None]:
    """Return ``(payload, truncated, overflow_path)`` for one output stream."""
    _, truncated = make_preview(text)
    if not truncated:
        return text, False, None
    result = await overflow_to_file(text, stream=stream)
    return f"{result.preview}\n\n{result.pointer}", True, result.path
