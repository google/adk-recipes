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

"""Opaque page token utilities for read tools.

Centralized token definitions ensure consistent encoding across the
command-route contract and prevent callers from depending on internal
offset structures.
"""

from __future__ import annotations

import base64
import json
import logging

logger = logging.getLogger(__name__)


def encode_page_token(offset: int) -> str:
    """Encodes a pagination offset as an opaque token.

    Args:
        offset: Zero-based item offset to encode.

    Returns:
        Opaque URL-safe base64-encoded token string.
    """
    return base64.urlsafe_b64encode(
        json.dumps({"offset": offset}).encode()
    ).decode()


def decode_page_token(token: str) -> int:
    """Decodes an opaque page token back to an item offset.

    Returns offset 0 when the token is missing or unparseable. Because tokens
    are opaque, callers cannot debug decoding failures directly, so defaulting
    to the first page provides a recoverable fallback.

    Args:
        token: Opaque base64-encoded token string.

    Returns:
        Non-negative item offset decoded from the token, or 0 if invalid.
    """
    if not token:
        return 0
    try:
        offset = json.loads(base64.urlsafe_b64decode(token.encode()).decode())[
            "offset"
        ]
        return max(0, int(offset))
    except Exception:
        logger.warning("Ignoring unparseable page_token; starting at 0.")
        return 0
