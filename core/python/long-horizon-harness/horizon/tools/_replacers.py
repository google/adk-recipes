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

"""Fuzzy-match replacer ladder for the ``edit`` tool.

Each replacer is a generator yielding candidate *search strings* that occur
verbatim in ``content`` — never edited output. ``find_replacement`` walks
the replacers in order (tightest first); the first replacer that yields a
usable candidate decides the outcome, applying the uniqueness / replace_all
rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass


def simple_replacer(content: str, find: str) -> Iterator[str]:
    yield find


def line_trimmed_replacer(content: str, find: str) -> Iterator[str]:
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if not search_lines:
        return

    for i in range(len(original_lines) - len(search_lines) + 1):
        matches = True
        for j in range(len(search_lines)):
            if original_lines[i + j].strip() != search_lines[j].strip():
                matches = False
                break
        if not matches:
            continue

        start = sum(len(original_lines[k]) + 1 for k in range(i))
        end = start
        for k in range(len(search_lines)):
            end += len(original_lines[i + k])
            if k < len(search_lines) - 1:
                end += 1
        yield content[start:end]


def whitespace_normalized_replacer(content: str, find: str) -> Iterator[str]:
    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    normalized_find = norm(find)
    lines = content.split("\n")

    for line in lines:
        if norm(line) == normalized_find:
            yield line
            continue
        if normalized_find and normalized_find in norm(line):
            words = find.strip().split()
            if words:
                pattern = r"\s+".join(re.escape(w) for w in words)
                m = re.search(pattern, line)
                if m:
                    yield m.group(0)

    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i : i + len(find_lines)])
            if norm(block) == normalized_find:
                yield block


def indentation_flexible_replacer(content: str, find: str) -> Iterator[str]:
    def remove_indent(text: str) -> str:
        lines = text.split("\n")
        non_empty = [ln for ln in lines if ln.strip()]
        if not non_empty:
            return text
        min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
        return "\n".join(
            ln if not ln.strip() else ln[min_indent:] for ln in lines
        )

    normalized_find = remove_indent(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    if len(content_lines) < len(find_lines):
        return

    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i : i + len(find_lines)])
        if remove_indent(block) == normalized_find:
            yield block


def block_anchor_replacer(content: str, find: str) -> Iterator[str]:
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if len(search_lines) < 3:
        return
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if len(search_lines) < 3:
        return

    first = search_lines[0].strip()
    last = search_lines[-1].strip()

    candidates: list[tuple[int, int]] = []
    for i in range(len(original_lines)):
        if original_lines[i].strip() != first:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last:
                candidates.append((i, j))
                break

    for start_line, end_line in candidates:
        start = sum(len(original_lines[k]) + 1 for k in range(start_line))
        end = start
        for k in range(start_line, end_line + 1):
            end += len(original_lines[k])
            if k < end_line:
                end += 1
        yield content[start:end]


REPLACERS = (
    simple_replacer,
    line_trimmed_replacer,
    whitespace_normalized_replacer,
    indentation_flexible_replacer,
    block_anchor_replacer,
)

_STRATEGY_NAMES = ", ".join(r.__name__ for r in REPLACERS)


@dataclass(frozen=True)
class Replacement:
    """Resolved ``old_string`` against file content.

    On success ``search`` is the verbatim slice of content to replace and
    ``count`` is how many times it will be replaced. On failure ``search``
    is ``None`` and ``error`` carries a model-facing message.
    """

    search: str | None
    count: int
    error: str | None


def find_replacement(content: str, old_string: str) -> Replacement:
    # The only caller (file_ops._resolve_edits) resolves one edits[] item at
    # a time and always wants exactly one match, so there is no replace_all
    # mode here — batching N occurrences means passing N edits.
    for replacer in REPLACERS:
        seen: list[str] = []
        for candidate in replacer(content, old_string):
            if candidate in content and candidate not in seen:
                seen.append(candidate)
        if not seen:
            continue

        if len(seen) > 1:
            return Replacement(
                None,
                0,
                f"oldText matched multiple distinct blocks via "
                f"{replacer.__name__}; the anchor must be unique. Add "
                "surrounding context, or split the change into more "
                "specific anchors.",
            )
        search = seen[0]
        occurrences = content.count(search)
        if occurrences > 1:
            return Replacement(
                None,
                0,
                f"oldText matched {occurrences} times in the file; the "
                "anchor must be unique. Add surrounding context, or split "
                "the change into more specific anchors.",
            )
        return Replacement(search, 1, None)

    return Replacement(
        None,
        0,
        "oldText not found. Tried, in order: "
        f"{_STRATEGY_NAMES}. It must match the file's text — check "
        "whitespace, indentation, and that the lines still exist.",
    )
