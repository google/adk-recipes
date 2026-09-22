# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utility functions for extracting and validating loan request IDs."""

import re

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)


def extract_request_id_from_text(text: str) -> str:
    """
    Extract loan_request_id from text using regex.

    Args:
        text: The text to search for a loan request ID.

    Returns:
        The extracted request ID in format SBL-YYYY-XXXXX.

    Raises:
        ValueError: If no valid request ID is found.
    """
    if not text:
        raise ValueError("No text provided to extract request ID from")

    match = re.search(r"SBL-\d{4}-\d{5}", text)

    if match:
        request_id = match.group(0)
        logger.info(f"Extracted loan_request_id: {request_id}")
        return request_id
    else:
        raise ValueError(
            "No loan_request_id found in message. "
            "Please provide an ID in the format SBL-YYYY-XXXXX (e.g., SBL-2025-00142).\n"
            "Examples:\n"
            '  - "Process this application for SBL-2025-00142"\n'
            '  - "What is the status on SBL-2025-00142?"\n'
            '  - "Resume processing for SBL-2025-00142"'
        )
