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

"""Parse a .env-style KEY=VALUE blob into individual secrets.

Pure and I/O-free: the single source of truth for both the preview and the
commit path of POST /lha/secrets/import. Malformed lines are skipped and
reported, never fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Must match _VALID_NAME in app/api/secrets.py (both gate the same secret-name contract).
_VALID_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Skipped:
    lineno: int
    reason: str


@dataclass(frozen=True)
class ParsedDotenv:
    entries: dict[str, str]
    skipped: list[Skipped]


def _unquote(value: str) -> str:
    stripped = value.strip()
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in ("'", '"')
    ):
        return stripped[1:-1]
    # Unquoted: drop an inline trailing comment (search before stripping so a
    # value that is only a comment becomes empty).
    hash_idx = value.find(" #")
    if hash_idx != -1:
        value = value[:hash_idx]
    return value.strip()


def parse_dotenv(text: str) -> ParsedDotenv:
    entries: dict[str, str] = {}
    skipped: list[Skipped] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            skipped.append(Skipped(lineno=lineno, reason="no '='"))
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _VALID_KEY.match(key):
            skipped.append(Skipped(lineno=lineno, reason="invalid key name"))
            continue
        entries[key] = _unquote(value)
    return ParsedDotenv(entries=entries, skipped=skipped)
