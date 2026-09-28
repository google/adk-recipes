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

"""Sanitize event content before it is replayed into Memory Bank's
``memories.generate``.

Memory Bank's server-side extractor (a Gemini model) ingests text plus
standard image/PDF media; it rejects any other ``inline_data`` mime with a
400 ("Unable to submit request because it has a mimeType parameter with
value ..., which is not supported"). Stored session events can legitimately
carry such blobs — a user uploads a ``.json`` file, or the ``read`` tool's
media path injects one — so the nightly dream pass must degrade them to a
short text placeholder before generation.
"""

from __future__ import annotations

from google.genai.types import Content, Part

# Inline-data mimes the Memory Bank extractor accepts. Everything else is
# degraded to a text placeholder so the generate request can't 400.
_EXTRACTABLE_PREFIXES = ("image/",)
_EXTRACTABLE_EXACT = frozenset({"application/pdf"})


# Deliberately separate from the read tool's media gate: tightening the
# Memory Bank extractor allowlist must not perturb what tools surface to
# the model.
def _extractor_can_ingest(mime: str | None) -> bool:
    if not mime:
        return False
    base = mime.split(";")[0].strip().lower()
    return base in _EXTRACTABLE_EXACT or any(
        base.startswith(p) for p in _EXTRACTABLE_PREFIXES
    )


def _placeholder(blob: object) -> Part:
    mime = getattr(blob, "mime_type", None) or "unknown"
    name = getattr(blob, "display_name", None)
    label = f"{mime} {name}".strip() if name else mime
    return Part(text=f"[attachment omitted: {label}]")


def sanitize_content_for_memory(content: Content | None) -> Content | None:
    if content is None:
        return None
    parts = getattr(content, "parts", None)
    if not parts:
        return content

    new_parts: list[Part] = []
    changed = False
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None and not _extractor_can_ingest(
            getattr(blob, "mime_type", None)
        ):
            new_parts.append(_placeholder(blob))
            changed = True
        else:
            new_parts.append(part)

    if not changed:
        return content
    return Content(role=content.role, parts=new_parts)


__all__ = ["sanitize_content_for_memory"]
