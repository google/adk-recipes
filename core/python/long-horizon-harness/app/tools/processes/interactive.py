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

"""Heuristic detection of interactive stdin prompts.

A process that's been quiet long enough AND whose output tail matches a
known prompt pattern is almost certainly waiting on stdin. The goal is
to surface this back to the LLM so it can either rerun the command with
non-interactive flags or drive the process via ``process(action='write')``.
"""

from __future__ import annotations

import re

INTERACTIVE_IDLE_SECONDS = 3.0
_INSPECT_WINDOW_BYTES = 512

_ANSI_ESCAPE_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)pass(word|phrase)\s*[:?]\s*\Z"),
    re.compile(r"(?i)pass(word|phrase)\s+for\s+\S+\s*[:?]\s*\Z"),
    re.compile(r"\[[yY]/[nN]\]\s*\Z"),
    re.compile(r"\([yY]es/[nN]o\)\??\s*\Z"),
    re.compile(r"\([yY]/[nN]\)\??\s*\Z"),
    re.compile(r"(?m)^>>>\s*\Z"),
    re.compile(r"(?m)^In\s*\[\d+\]:\s*\Z"),
    re.compile(r"(?m)^\(Pdb\)\s*\Z"),
)


def _strip_ansi(data: bytes) -> bytes:
    return _ANSI_ESCAPE_RE.sub(b"", data)


def detect_interactive_prompt(
    tail: bytes, *, idle_seconds: float
) -> str | None:
    """Return the matched prompt fragment, or None.

    Fires only when ``idle_seconds`` >= ``INTERACTIVE_IDLE_SECONDS`` AND the
    last ``_INSPECT_WINDOW_BYTES`` of ``tail`` (ANSI-stripped) matches one of
    the known prompt patterns at end-of-buffer.
    """
    if idle_seconds < INTERACTIVE_IDLE_SECONDS:
        return None
    if not tail:
        return None

    window = _strip_ansi(tail[-_INSPECT_WINDOW_BYTES:])
    try:
        text = window.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Normalize trailing CRs from PTYs.
    normalized = text.replace("\r\n", "\n").rstrip("\r")
    for pattern in _PROMPT_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return match.group(0)
    return None
