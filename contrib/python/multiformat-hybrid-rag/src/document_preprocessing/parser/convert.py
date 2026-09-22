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

"""File format conversion utilities using LibreOffice headless."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _find_libreoffice() -> str:
    """Find the LibreOffice binary (handles macOS vs Linux paths)."""
    for name in ("libreoffice", "soffice"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError(
        "LibreOffice not found. Install it with: brew install libreoffice"
    )


def to_format(file_path: str, fmt: str = "pdf") -> str:
    """Convert a document to a target format using LibreOffice headless.

    Args:
        file_path: Path to the source file (.ppt, .pptx, .doc, .docx, .rtf, etc.)
        fmt: Target format (default: "pdf"). Examples: "pdf", "pptx", "docx".

    Returns:
        Path to the converted file in a temp directory.

    Raises:
        RuntimeError: If LibreOffice conversion fails.
    """
    src = Path(file_path)
    outdir = tempfile.mkdtemp(prefix="lo_convert_")

    lo_bin = _find_libreoffice()
    result = subprocess.run(
        [
            lo_bin,
            "--headless",
            "--convert-to",
            fmt,
            "--outdir",
            outdir,
            str(src),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    out_path = Path(outdir) / f"{src.stem}.{fmt}"
    if not out_path.exists():
        raise RuntimeError(
            f"LibreOffice conversion failed for {src.name}: {result.stderr}"
        )

    logger.info("Converted %s -> %s", src.name, out_path)
    return str(out_path)


def to_pdf(file_path: str) -> str:
    """Convenience wrapper: convert any document to PDF."""
    return to_format(file_path, "pdf")


def parse_via_pdf(
    file_path: str, genai_client=None, max_workers: int = 8
) -> str:
    """Convert a document to PDF via LibreOffice, then parse with the PDF parser.

    Used by .doc, .docx, .rtf parsers — they all share this exact flow.
    """
    # Local import to avoid a circular dependency: pdf_parser doesn't
    # import convert, but we'd otherwise pull pdf_parser at module load
    # for a function that's only called at parse time.
    from src.document_preprocessing.parser.pdf_parser import parse as parse_pdf

    pdf_path = to_pdf(file_path)
    return parse_pdf(
        pdf_path, genai_client=genai_client, max_workers=max_workers
    )
