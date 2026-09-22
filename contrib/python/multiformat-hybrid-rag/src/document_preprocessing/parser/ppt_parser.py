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

"""Legacy .ppt → Markdown parser.

Converts to .pptx first (for raw text extraction), then uses the pptx_parser flow.
"""

from __future__ import annotations

import logging

from src.document_preprocessing.parser.convert import to_format
from src.document_preprocessing.parser.pptx_parser import parse as parse_pptx

logger = logging.getLogger(__name__)


def parse(file_path: str, genai_client=None, max_workers: int = 8) -> str:
    """Extract markdown from a .ppt file.

    1. Convert .ppt → .pptx via LibreOffice (preserves raw text)
    2. Delegate to pptx_parser (which converts to PDF + extracts raw text)
    """
    pptx_path = to_format(file_path, "pptx")
    return parse_pptx(
        pptx_path, genai_client=genai_client, max_workers=max_workers
    )
