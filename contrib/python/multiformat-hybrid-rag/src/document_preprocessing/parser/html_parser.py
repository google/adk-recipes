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

"""HTML → Markdown parser.

Strips non-content elements with BeautifulSoup (scripts, styles, nav,
footer, images, SVGs, hidden elements, HTML attributes, empty tags),
then sends the cleaned HTML to Gemini Flash for markdown conversion.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Comment

from src.utils.config import config as pipeline_config
from src.utils.gemini import generate_gemini
from src.utils.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)

MIN_WORDS = 50
MIN_SENTENCES = 2

_STRIP_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "link",
    "meta",
    "head",
    "iframe",
    "picture",
    "source",
    "img",
}
_STRIP_STRUCTURAL = {"nav", "footer", "header"}

SYSTEM_PROMPT = """\
Convert the following HTML into clean Markdown.

Rules:
- Keep ALL textual content — do not drop or summarise anything
- Preserve structure: headings, lists, tables, bold/italic
- Remove navigation menus, cookie banners, social media links, and other boilerplate
- Output only the Markdown, nothing else"""


def _clean_html(raw: str) -> str:
    """Strip non-content elements from raw HTML."""
    soup = BeautifulSoup(raw, "lxml")

    for tag in soup.find_all(_STRIP_TAGS | _STRIP_STRUCTURAL):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    for tag in soup.find_all(
        attrs={"style": lambda v: v and "display:none" in v.replace(" ", "")}
    ):
        tag.decompose()
    for tag in soup.find_all(attrs={"hidden": True}):
        tag.decompose()

    # Strip all HTML attributes (reduces noise for the LLM)
    for tag in soup.find_all(True):
        tag.attrs = {}

    # Remove empty tags
    for tag in soup.find_all(True):
        if not tag.get_text(strip=True) and tag.name not in ("br", "hr"):
            tag.decompose()

    return str(soup)


def _has_content(text: str) -> bool:
    """Check if extracted text has meaningful content."""
    word_count = len(text.split())
    sentences = len(re.findall(r"[.!?](?:\s|$)", text))
    return word_count >= MIN_WORDS and sentences >= MIN_SENTENCES


def parse(file_path: str, genai_client=None, **_kwargs) -> str:
    """Extract markdown from an HTML file.

    1. Read and clean HTML with BeautifulSoup
    2. Check body has enough content (50+ words, 2+ sentences)
    3. Send cleaned HTML to Gemini Flash for markdown conversion
    """
    if genai_client is None:
        raise ValueError("genai_client is required for HTML parsing")

    with open(file_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()

    cleaned = _clean_html(raw)

    # Extract plain text to check content threshold
    soup = BeautifulSoup(cleaned, "lxml")
    body_text = soup.get_text(separator="\n", strip=True)

    if not _has_content(body_text):
        logger.info("Skipping %s — insufficient body content", file_path)
        return ""

    gen_config = get_generate_content_config(
        temperature=0,
        response_modalities=["TEXT"],
        system_instruction=SYSTEM_PROMPT,
    )

    return generate_gemini(
        content_pieces=[cleaned],
        client=genai_client,
        config=gen_config,
        model=pipeline_config.markdown_converter_gemini_model,
    )


def quick_raw_text(file_path: str) -> str:
    """Cheap raw-text extraction (no LLM) for early relevance filtering.

    BeautifulSoup strips scripts/styles/nav/img, then `.get_text()` returns
    the visible text. Cookie banners and login pages produce very little
    text by design, so the caller's length threshold catches them.
    """
    with open(file_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    cleaned = _clean_html(raw)
    return BeautifulSoup(cleaned, "lxml").get_text(separator=" ", strip=True)
