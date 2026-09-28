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

"""Recover ADK's opaque A2A DataPart blobs as readable text.

When an A2A ``DataPart`` carries no ADK metadata-type key (an untyped
client-sent payload), ADK's ``convert_a2a_part_to_genai_part`` wraps its JSON in a
``<a2a_datapart_json>...</a2a_datapart_json>`` ``text/plain`` ``inline_data``
blob. Some model adapters have no branch for such blobs and can error on them,
and even when tolerated the tagged blob is opaque to the model. These pure
helpers let the inbound converter turn that blob back into text the model can
actually read.

Tags mirror ``google.adk.a2a.converters.part_converter``; kept inline so this
module stays dependency-free (no A2A SDK import).
"""

from __future__ import annotations

import json

A2A_DATA_PART_START_TAG = b"<a2a_datapart_json>"
A2A_DATA_PART_END_TAG = b"</a2a_datapart_json>"

# Discriminator the web client stamps on a highlighted-selection payload.
# Mirrors SELECTION_DATA_KIND in web/components/chat/workspace-href.ts.
_SELECTION_KIND = "lha.selection"

# Discriminator for files dragged from the workspace tree into the composer.
# Mirrors FILEREF_DATA_KIND in web/components/chat/workspace-href.ts.
_FILEREF_KIND = "lha.fileref"


def _render_fileref(payload: dict) -> str | None:
    """Prose for workspace files the user attached from the tree.

    Only paths travel: the files are already in the workspace, so re-sending
    their bytes would duplicate them. Says what was pointed at, not what to do
    with it — the user's own message carries the instruction.
    """
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    clean = [p for p in paths if isinstance(p, str) and p]
    if len(clean) != len(paths):
        return None
    listed = "\n".join(f"- {p}" for p in clean)
    noun = "file" if len(clean) == 1 else "files"
    return (
        f"[The user attached {len(clean)} workspace {noun} to this message. "
        "Read them if the request concerns them:]\n"
        f"{listed}"
    )


def _render_selection(payload: dict) -> str | None:
    """Prose for a text selection the user highlighted in the web UI.

    ``edit`` has no character-offset targeting, so unlike the earlier
    `patch` tool this asks the model to search with the
    highlighted text plus enough surrounding context to match only this
    location, rather than handing over ``start``/``end``. Deliberately states
    what was selected, not what to do with it — the user's own message
    carries the instruction. ``start``/``end`` are still required in the
    payload as a shape check that this really is a selection event, even
    though the rendered text no longer surfaces them.
    """
    path = payload.get("path")
    snippet = payload.get("snippet")
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(path, str) or not isinstance(snippet, str):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    start_line = payload.get("start_line")
    end_line = payload.get("end_line")
    where = path
    if isinstance(start_line, int) and isinstance(end_line, int):
        where += (
            f", line {start_line}"
            if start_line == end_line
            else f", lines {start_line}-{end_line}"
        )
    return (
        f"[The user highlighted this exact text in {where}:]\n"
        f"{snippet}\n"
        "[To act on exactly that span, call edit with "
        "edits=[{'oldText': <the highlighted text above, plus enough "
        "surrounding context from the file to make it match only this "
        "location>, 'newText': ...}].]"
    )


def unwrap_a2a_datapart_text(data: bytes | None) -> str | None:
    """Return readable text for a wrapped DataPart blob, else ``None``.

    ``None`` signals "not one of these blobs" so callers fall back to their
    own handling (placeholder substitution, pass-through, etc.).
    """
    if not data or not data.startswith(A2A_DATA_PART_START_TAG):
        return None
    body = data[len(A2A_DATA_PART_START_TAG) :]
    if body.endswith(A2A_DATA_PART_END_TAG):
        body = body[: -len(A2A_DATA_PART_END_TAG)]
    try:
        obj = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    payload = obj.get("data", obj) if isinstance(obj, dict) else obj
    if isinstance(payload, dict):
        kind = payload.get("lha_kind")
        rendered = None
        if kind == _SELECTION_KIND:
            rendered = _render_selection(payload)
        elif kind == _FILEREF_KIND:
            rendered = _render_fileref(payload)
        if rendered is not None:
            return rendered
    return "[UI action] " + json.dumps(payload, ensure_ascii=False)


__all__ = [
    "A2A_DATA_PART_END_TAG",
    "A2A_DATA_PART_START_TAG",
    "unwrap_a2a_datapart_text",
]
