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

"""Markdown to plain text, for words that are spoken or shown as a card line."""

from __future__ import annotations

import re

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"\(?https?://[^\s)]+\)?")
_HEADING = re.compile(r"^#+\s*")
_BULLET = re.compile(r"^(?:[-*+•]|\d+[.)])\s+")
# Underscores inside identifiers (test_retry.py, DATABASE_URL) aren't emphasis.
_EMPHASIS = re.compile(r"[*`]{1,2}|(?<!\w)__?|__?(?!\w)")
_BLOCK_MARKERS = ("```", "|", "---")
# Grounding citation marks: [1], [1.33], [2, 5].
_CITATION = re.compile(r"\s*\[\d+(?:\.\d+)?(?:,\s*\d+(?:\.\d+)?)*\]")


def drop_links(text: str) -> str:
    return _URL.sub("", _LINK.sub(r"\1", text))


def markdown_lines(text: str) -> list[str]:
    return [
        _HEADING.sub("", line).strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(_BLOCK_MARKERS)
    ]


def strip_emphasis(text: str) -> str:
    return _EMPHASIS.sub("", text)


def plain_text(text: str) -> str:
    lines = [
        strip_emphasis(_BULLET.sub("", line))
        for line in markdown_lines(_CITATION.sub("", drop_links(text)))
    ]
    return " ".join(" ".join(lines).split())
