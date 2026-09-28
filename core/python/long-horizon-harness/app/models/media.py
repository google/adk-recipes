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

"""Media-input helpers shared by the ``read`` tool's media path (media
gating) and ``file_ops`` (mime sniffing).

Kept dependency-free (stdlib only) so importing them never pulls in a model SDK.
"""

from __future__ import annotations


def b64_len(n_raw: int) -> int:
    """Exact base64 length for ``n_raw`` bytes — no allocation. Standard base64
    emits 4 chars per 3 input bytes (padded), which is what model APIs measure."""
    return 4 * ((n_raw + 2) // 3)


def sniff_image_or_pdf_mime(data: bytes | None) -> str | None:
    """Best-effort magic-byte detection for common image/PDF formats.

    Recovers blobs whose declared mime is wrong or missing (e.g. browser
    uploads with an empty ``File.type``) so a real image/PDF is classified
    correctly instead of falling back to an extension guess.
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


__all__ = [
    "b64_len",
    "sniff_image_or_pdf_mime",
]
