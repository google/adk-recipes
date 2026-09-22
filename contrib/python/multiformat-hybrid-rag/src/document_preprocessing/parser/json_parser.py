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

"""JSON / JSONL → Markdown parser.

Schema-agnostic: walks any JSON tree and emits markdown without needing
to know the source schema in advance. This matters because the source
data can change without warning — new catalog files, new fields — and
hand-coded adapters would break.

Output strategy:
- Object  → `**key**: value` lines, one per non-empty field; nested
            objects/arrays render as indented sub-sections under their
            own heading.
- Array of objects → each item rendered as a `## {id-or-name}` section
            separated by `---`. The MarkdownTextSplitter prefers these
            boundaries, so the chunker tends to keep one item per chunk.
- Array of primitives → bullet list.
- JSONL → treated as a single array of records (one per line).

Empty fields (None, "", [], {}) are skipped so the output stays
readable instead of pages of `**field**:` lines.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Fields we try in order to pick a human-readable heading for each item
# when the input is an array of objects. First non-empty match wins.
# Common identifier fields across various schemas.
# Anything else falls through to "Item N".
_HEADING_CANDIDATES = (
    "name",
    "title",
    "nomeOfferta",
    "finOfferBoxTitle",
    "id",
    "idNmu",
    "finOfferId",
)

_MAX_HEADING_DEPTH = 6  # markdown tops out at h6


def _is_empty(value) -> bool:
    """True if a value carries no information worth rendering."""
    return value is None or value in ("", [], {})


def _heading_for(item: dict, fallback_idx: int) -> str:
    """Pick a heading for an item from common identifier fields."""
    for key in _HEADING_CANDIDATES:
        v = item.get(key)
        if v and isinstance(v, (str, int, float)):
            return str(v)
    return f"Item {fallback_idx}"


def _render(value, depth: int = 0) -> str:
    """Recursive markdown rendering. depth controls heading level for
    nested structures (so a top-level object's nested keys get h2, the
    next level h3, etc., capped at h6)."""
    if _is_empty(value):
        return ""

    if isinstance(value, dict):
        return _render_object(value, depth)
    if isinstance(value, list):
        return _render_array(value, depth)
    # primitives — keep newlines collapsed so the parent line stays clean
    return str(value).replace("\n", " ").strip()


def _render_object(obj: dict, depth: int) -> str:
    """Render a JSON object as markdown.

    Scalar fields → `**key**: value` lines.
    Nested objects/arrays → sub-section under their own heading.
    """
    scalar_lines: list[str] = []
    nested_blocks: list[str] = []

    for key, value in obj.items():
        if _is_empty(value):
            continue
        if isinstance(value, (dict, list)):
            heading_level = "#" * min(depth + 2, _MAX_HEADING_DEPTH)
            block = _render(value, depth + 1)
            if block:
                nested_blocks.append(f"{heading_level} {key}\n{block}")
        else:
            scalar_lines.append(f"**{key}**: {_render(value, depth)}")

    parts = []
    if scalar_lines:
        parts.append("\n".join(scalar_lines))
    if nested_blocks:
        parts.append("\n\n".join(nested_blocks))
    return "\n\n".join(parts)


def _render_array(items: list, depth: int) -> str:
    """Render a JSON array as markdown.

    Array of objects   → each item is a `## {heading}` section separated
                         by `---` so the chunker can split per-item.
    Array of primitives → bullet list.
    """
    if not items:
        return ""

    if isinstance(items[0], dict):
        sections: list[str] = []
        for idx, item in enumerate(items, start=1):
            heading = _heading_for(item, idx)
            body = _render_object(item, depth + 1)
            if body:
                sections.append(f"## {heading}\n\n{body}")
        return "\n\n---\n\n".join(sections)

    return "\n".join(
        f"- {_render(item, depth)}" for item in items if not _is_empty(item)
    )


def _load_records(file_path: Path):
    """Load a JSON or JSONL file. Returns the parsed Python value.

    For .jsonl, returns a list of one record per non-empty line.
    """
    if file_path.suffix.lower() == ".jsonl":
        records = []
        with file_path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    records.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    logger.warning(
                        "%s:%d skipping malformed JSONL line: %s",
                        file_path.name,
                        lineno,
                        e,
                    )
        return records

    with file_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse(file_path: str, **_kwargs) -> str:
    """Render a JSON/JSONL file as markdown for downstream chunking."""
    path = Path(file_path)
    data = _load_records(path)
    return _render(data, depth=0)
