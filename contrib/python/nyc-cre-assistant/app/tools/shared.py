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
"""Shared public data helpers."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

JsonRecord = dict[str, Any]


def text(value: Any) -> str | None:
    """Return a stripped string, or None for empty/null values."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def number_value(value: Any) -> int | float | None:
    """Return a numeric value when the source field is numeric."""
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def compact_address(parts: list[str | None]) -> str | None:
    """Join address parts while dropping blanks."""
    joined = ", ".join(part for part in parts if part).strip()
    return joined or None


def split_bbl(bbl: str) -> dict[str, str]:
    """Split a 10-digit BBL into Socrata borough/block/lot parts."""
    if not is_valid_bbl(bbl):
        raise ValueError("BBL must be exactly 10 digits.")
    return {
        "borough": str(int(bbl[0:1])),
        "block": str(int(bbl[1:6])),
        "lot": str(int(bbl[6:10])),
    }


def is_valid_bbl(bbl: str) -> bool:
    """Return whether a value is a 10-digit NYC BBL."""
    return isinstance(bbl, str) and len(bbl) == 10 and bbl.isdigit()


def query_json(url: str, headers: dict[str, str] | None = None) -> Any:
    """Fetch JSON from a public API."""
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = f": {body[:500]}" if body else ""
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}{detail}") from exc


def query_socrata(
    domain: str, dataset_id: str, params: dict[str, str]
) -> list[JsonRecord]:
    """Query a Socrata dataset and return list records."""
    url = f"https://{domain}/resource/{dataset_id}.json?{urlencode(params)}"
    parsed = query_json(url)
    if not isinstance(parsed, list):
        raise RuntimeError(
            f"Socrata {dataset_id} returned a non-list response."
        )
    return [record for record in parsed if isinstance(record, dict)]
