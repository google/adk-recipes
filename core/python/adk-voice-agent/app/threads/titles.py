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

"""Short human-readable thread titles, with a first-clause fallback from the goal."""

from __future__ import annotations

import re

MAX_TITLE_CHARS = 60
FALLBACK_WORDS = 6
UNTITLED = "Untitled thread"

_FILLER = re.compile(
    r"^(?:(?:hey|ok(?:ay)?|so|voice\s+agent)[,!]?\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|please\s+"
    r"|i\s+(?:want|need|would\s+like|'d\s+like)\s+(?:you\s+)?to\s+"
    r"|help\s+me\s+(?:to\s+)?"
    r"|let'?s\s+)*",
    re.IGNORECASE,
)
_CLAUSE_END = re.compile(r"[.!?;:\n]|,\s|\s[-\u2013\u2014]\s|\s\(")
_EDGES = " \"'`.,;:!?-\u2013\u2014\u201c\u201d\u2018\u2019"
_TRAILING = " ,;:-\u2013\u2014"
_DANGLING = frozenset(
    "a an and at by for from in into of on or the to with about".split()
)


def thread_title(title: str | None, goal: str) -> str:
    return clean_title(title or "") or fallback_title(goal)


def clean_title(title: str) -> str:
    text = " ".join(title.split()).strip(_EDGES)
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(_TRAILING)
    return text[:1].upper() + text[1:]


def fallback_title(goal: str) -> str:
    """Extract the first clause of `goal`, trimmed to `FALLBACK_WORDS`."""
    text = _FILLER.sub("", " ".join(goal.split()))
    clause = _CLAUSE_END.split(text, maxsplit=1)[0]
    words = clause.split()[:FALLBACK_WORDS]
    while len(words) > 1 and words[-1].lower() in _DANGLING:
        words.pop()
    return clean_title(" ".join(words)) or clean_title(goal) or UNTITLED
