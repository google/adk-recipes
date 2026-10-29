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

"""Lenient parsing of the JSON a model returns (used by ``core.gen``)."""

import json
import re

__all__ = ["UnparsableJSON", "parse_json"]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class UnparsableJSON(Exception):
    """The text holds no JSON object or array; ``text`` is all of it."""

    def __init__(self, message: str, text: str = ""):
        super().__init__(message)
        self.text = text


def parse_json(text: str) -> dict | list:
    """Lenient JSON: strip code fences, keep the first {...} or [...] block."""
    s = _FENCE_RE.sub("", text).strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = s.rfind("}" if s[start] == "{" else "]")
        s = s[start : end + 1] if end > start else s
    for candidate in (s, _TRAILING_COMMA_RE.sub(r"\1", s)):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict | list):
            return value
    raise UnparsableJSON(f"unparsable JSON: {text[:200]!r}", text)
