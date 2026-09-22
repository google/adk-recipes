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

"""PDF → Markdown parser using Gemini multimodal.

Core function is parse_pdf_pages() which processes pages in parallel,
optionally using raw text for validation.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pymupdf

from src.utils.config import config
from src.utils.gemini import generate_gemini
from src.utils.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an expert document analyst. You receive a PDF page and optionally "
    "some raw text extracted from the source file.\n\n"
    "Your task:\n"
    "- Convert the content to well-structured markdown\n"
    "- Preserve the original layout, headings, bullet points, tables, and formatting\n"
    "- The raw text may be incomplete (e.g. missing table headers or labels). "
    "Use it only to double-check spelling and wording of text that appears in both "
    "the PDF and the raw text. Trust the PDF as the primary source.\n"
    "- Do not summarize or omit any content — extract everything faithfully\n"
    "- Do NOT include:\n"
    "  * Footer/header templates, page numbers\n"
    "  * Logos, branding, or company identity information (e.g. 'Materiale realizzato da...', company names)\n"
    "  * Decorative separators: repeated dots, dashes, equals, or underscores used as "
    "    visual borders/lines (e.g. '..................', '————', '====', '_______')\n"
    "  * Form field placeholders that don't convey content\n"
    "  * Note: Structural markdown (table pipes |, horizontal rules ---, table separators) "
    "    is allowed and necessary for tables\n"
    "- Use proper markdown syntax: # for headings, - or * for bullets, | for tables, "
    "**bold**, *italic*\n"
    "- When an image is present that conveys information (charts, diagrams, screenshots), "
    "insert a markdown image link with a descriptive alt text: "
    "![Description](image_placeholder.png). Skip decorative images and logos.\n"
    "- Output only the markdown, no preamble or explanation"
)


def _process_page(args: tuple) -> str:
    """Process a single PDF page with Gemini."""
    i, total, page_pdf, raw_text, genai_client, gen_config = args
    if raw_text:
        prompt = (
            "Here is a PDF page and some raw text extracted from the source file. "
            "The raw text may be incomplete (e.g. missing table headers). "
            "Use it only to double-check spelling of text visible in the PDF.\n\n"
            f"--- RAW TEXT (may be incomplete) ---\n{raw_text}\n--- END RAW TEXT ---\n\n"
            "Convert this page to markdown."
        )
    else:
        prompt = "Convert this page to markdown."

    logger.info("Processing page %d/%d", i + 1, total)

    return generate_gemini(
        content_pieces=[page_pdf, prompt],
        client=genai_client,
        config=gen_config,
        model=config.markdown_converter_gemini_model,
    )


def _split_pdf_pages(pdf_path: str) -> tuple[list[bytes], list[str]]:
    """Split a PDF into individual page bytes and extract raw text per page."""
    doc = pymupdf.open(pdf_path)
    pages = []
    texts = []
    for i in range(len(doc)):
        page = doc.load_page(i)
        texts.append(page.get_text())
        single = pymupdf.open()
        single.insert_pdf(doc, from_page=i, to_page=i)
        pages.append(single.tobytes())
        single.close()
    doc.close()
    return pages, texts


def parse_pdf_pages(
    pages: list[bytes],
    genai_client,
    texts: list[str] | None = None,
    max_workers: int = 8,
) -> str:
    """Process PDF pages in parallel with Gemini.

    Args:
        pages: List of single-page PDF bytes.
        genai_client: Gemini client instance.
        texts: Optional list of raw text per page for validation.
            If provided, used as authoritative text source.
        max_workers: Number of parallel Gemini calls.

    Returns:
        Merged markdown string with --- separators between pages.
    """
    gen_config = get_generate_content_config(
        temperature=0,
        response_modalities=["TEXT"],
        system_instruction=SYSTEM_PROMPT,
    )

    total = len(pages)

    # Build args list
    if texts is None:
        texts = [None] * total
    args_list = [
        (
            i,
            total,
            page,
            texts[i] if i < len(texts) else None,
            genai_client,
            gen_config,
        )
        for i, page in enumerate(pages)
    ]

    # Process in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_process_page, args_list))

    return "\n\n---\n\n".join(results)


def parse(file_path: str, genai_client=None, max_workers: int = 8) -> str:
    """Extract markdown from a PDF file.

    Splits into pages and sends to Gemini in parallel.
    """
    if genai_client is None:
        raise ValueError("genai_client is required for PDF parsing")

    pages, texts = _split_pdf_pages(file_path)
    return parse_pdf_pages(
        pages, genai_client, texts=texts, max_workers=max_workers
    )


def quick_raw_text(file_path: str) -> str:
    """Cheap raw-text extraction (no LLM) for early relevance filtering.

    Uses pymupdf's text extraction. Returns "" for scanned PDFs (image-only
    pages have no embedded text), which is the signal the caller uses to
    fall through to full Gemini extraction.
    """
    with pymupdf.open(file_path) as doc:
        return "\n".join(page.get_text() for page in doc)
