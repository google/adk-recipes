# Copyright 2025 Google LLC
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

"""Shared utilities for Gemini image generation (nano/banana).

Supports both Flash Image Preview and Pro models.
"""

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError

# Third-party imports
from google.genai import types
from google.genai.errors import ClientError

# Project imports
from workflows.shared.llm_utils import (
    get_generate_content_config,
    get_part,
    retry_with_exponential_backoff,
)

logger = logging.getLogger(__name__)

# Default timeout for nano generation (seconds)
NANO_TIMEOUT_SECONDS = 90


class NanoTimeoutError(Exception):
    """Raised when nano generation times out."""

    pass


@retry_with_exponential_backoff(
    max_retries=20,
    initial_delay=5.0,  # Start with 5s delay for 429 errors
    exponential_base=2.0,  # Delays: 5s, 10s, 20s, 40s, 60s (capped), then 60s per retry
    max_delay=60.0,
    exceptions=(NanoTimeoutError, ClientError),
)
def generate_nano(
    client,
    text_images_pieces,
    model=os.getenv("MODEL_NAME_GENERATED_5", "gemini-3.6-flash"),  # noqa: B008
    config=None,
    timeout=NANO_TIMEOUT_SECONDS,
):
    """Generate content using Gemini image generation model.

    Args:
        client: Gemini client instance
        text_images_pieces: List of text strings and/or image bytes
        model: Model to use (default: "gemini-3.1-flash-image")
        config: Optional GenerateContentConfig (uses default if None)
        timeout: Timeout in seconds (default: 90). Set to None to disable.

    Returns:
        bytes: Generated image data

    Raises:
        NanoTimeoutError: If generation takes longer than timeout

    """
    thread_id = threading.current_thread().name
    logger.debug(
        f"[generate_nano] Thread {thread_id}: Preparing API call to {model}"
    )

    parts = [get_part(x) for x in text_images_pieces]
    contents = [types.Content(role="user", parts=parts)]

    if config is None:
        config = get_generate_content_config(
            response_modalities=["IMAGE", "TEXT"]
        )

    def _call_api():
        return client.models.generate_content(
            model=model, contents=contents, config=config
        )

    # If no timeout, call directly
    if timeout is None:
        logger.debug(
            f"[generate_nano] Thread {thread_id}: Calling generate_content (no timeout)..."
        )
        result = _call_api()
    else:
        # Use ThreadPoolExecutor to enforce timeout.
        # The API call runs in a worker thread; we wait up to `timeout` seconds.
        # If timeout is reached, we stop waiting and raise NanoTimeoutError.
        # Note: The worker thread may continue running in the background.
        logger.debug(
            f"[generate_nano] Thread {thread_id}: Calling generate_content (timeout: {timeout}s)..."
        )
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_call_api)
            result = future.result(timeout=timeout)
        except FuturesTimeoutError:
            logger.error(
                f"[generate_nano] Thread {thread_id}: API call timed out after {timeout}s"
            )
            # Shutdown without waiting - let the background thread continue/die on its own
            executor.shutdown(wait=False)
            raise NanoTimeoutError(
                f"Nano generation timed out after {timeout} seconds"
            ) from None
        finally:
            # Only wait for cleanup if we didn't timeout
            executor.shutdown(wait=False)

    logger.debug(f"[generate_nano] Thread {thread_id}: API call completed")

    result = result.candidates[0].content.parts
    result = next(x for x in result if x.text is None).inline_data.data
    return result
