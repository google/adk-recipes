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

"""Document relevance classifier for the knowledge base.

Checks whether an extracted document contains useful information.
Filters out empty pages, login screens, navigation-only HTML, cookie
banners, and other non-informational content that would pollute the
vector index.

Irrelevant documents are still recorded in the preprocessed table
(with relevant=FALSE) so they are not re-evaluated on subsequent runs.
"""

from __future__ import annotations

import logging

from src.utils.config import config as pipeline_config
from src.utils.gemini import generate_gemini
from src.utils.llm_utils import get_generate_content_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a content quality filter for a knowledge base.

Your task: given the extracted text of a document, determine whether it \
contains useful, substantive information worth indexing.

A document is NOT RELEVANT if it:
- Is empty or contains only boilerplate (headers, footers, navigation menus)
- Contains only login forms, cookie consent, or authentication pages
- Is purely visual with no textual information (image placeholders only)
- Contains only metadata, URLs, or code with no human-readable content

In all the other cases, the document must be considered RELEVANT.

Respond with exactly one word: RELEVANT or IRRELEVANT"""


def classify_relevance(
    content: str,
    genai_client,
) -> bool:
    """Classify whether a document is relevant for the knowledge base.

    Args:
        content: The extracted text content of the document.
        genai_client: Gemini client instance.

    Returns:
        True if the document is relevant, False otherwise.
    """
    # Skip very short documents — almost certainly not useful
    if len(content.strip()) < 50:
        logger.info(
            "Document too short (%d chars), marking irrelevant.",
            len(content.strip()),
        )
        return False

    gen_config = get_generate_content_config(
        temperature=0,
        response_modalities=["TEXT"],
        system_instruction=SYSTEM_PROMPT,
    )

    prompt = f"Document text:\n```\n{content}\n```"

    try:
        result = generate_gemini(
            content_pieces=[prompt],
            client=genai_client,
            config=gen_config,
            model=pipeline_config.relevance_gemini_model,
        )
        upper = result.upper().strip()
        if "IRRELEVANT" in upper:
            is_relevant = False
        elif "RELEVANT" in upper:
            is_relevant = True
        else:
            logger.warning(
                "Unexpected classifier output: %s — defaulting to relevant",
                result.strip(),
            )
            is_relevant = True
        logger.info(
            "Relevance: %s (%s)",
            "RELEVANT" if is_relevant else "IRRELEVANT",
            result.strip(),
        )
        return is_relevant
    except Exception as e:
        # On failure, default to relevant (don't lose data)
        logger.warning("Relevance check failed, defaulting to relevant: %s", e)
        return True
