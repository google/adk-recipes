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

"""Document parsing — extract markdown from various file formats.

Each parser module exposes a `parse(file_path, ...) -> str` function
that returns the document content as markdown text.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.document_preprocessing.parser import (
    doc_parser,
    docx_parser,
    html_parser,
    json_parser,
    pdf_parser,
    ppt_parser,
    pptx_parser,
    rtf_parser,
    xls_parser,
    xlsx_parser,
)
from src.utils.llm_utils import get_mime_type_from_path

logger = logging.getLogger(__name__)

PARSEABLE_MIMES = {
    "text/html",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/rtf",
    "application/json",
    "application/jsonl",
}

_PARSER_MAP = {
    "text/html": html_parser,
    "application/pdf": pdf_parser,
    "application/msword": doc_parser,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": docx_parser,
    "application/vnd.ms-powerpoint": ppt_parser,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": pptx_parser,
    "application/vnd.ms-excel": xls_parser,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": xlsx_parser,
    "application/rtf": rtf_parser,
    "application/json": json_parser,
    "application/jsonl": json_parser,
}


def parse(
    file_path: str | Path, genai_client=None, max_workers: int = 8
) -> str:
    """Parse a document file to markdown.

    Args:
        file_path: Local path to the file.
        genai_client: Gemini client (needed for PDF/legacy Office formats).
        max_workers: Max parallel Gemini calls per page.

    Returns:
        Extracted markdown text.

    Raises:
        ValueError: If the file format is unsupported.
    """
    path = Path(file_path)
    mime = get_mime_type_from_path(str(path))
    parser = _PARSER_MAP.get(mime)
    if parser is None:
        if mime in ("text/markdown", "text/plain"):
            return path.read_text(encoding="utf-8", errors="replace")
        raise ValueError(f"Unsupported format: {path.suffix} ({mime})")

    return parser.parse(
        str(path), genai_client=genai_client, max_workers=max_workers
    )


def quick_raw_text(file_path: str | Path) -> str | None:
    """Cheap raw-text extraction without invoking any LLM.

    Used by the preprocess service to run the relevance classifier on
    raw text BEFORE the expensive markdown conversion. If we can confirm
    a file is irrelevant from raw text alone, we skip the full Gemini
    call(s).

    Returns:
        - str (possibly empty) if the parser supports cheap extraction.
          An empty/sparse return signals "couldn't extract usable text"
          (e.g. scanned PDF), so the caller should fall through to full
          extraction instead of pre-classifying.
        - None if the parser has no quick-extract path. Caller falls
          through to full extraction.
    """
    path = Path(file_path)
    mime = get_mime_type_from_path(str(path))
    parser = _PARSER_MAP.get(mime)
    if parser is None or not hasattr(parser, "quick_raw_text"):
        return None
    try:
        return parser.quick_raw_text(str(path))
    except Exception as e:
        logger.warning("quick_raw_text failed for %s: %s", path.name, e)
        return None
