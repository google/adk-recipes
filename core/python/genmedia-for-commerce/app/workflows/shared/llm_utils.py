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

"""Shared LLM utilities for creating Gemini content parts.

Used by both text generation (gemini) and image generation (nano_banana).
"""

# Standard library imports
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx

# Third-party imports
from google.genai import types
from google.genai.errors import ClientError

logger = logging.getLogger(__name__)

# Default retryable exceptions: API errors + transient network/SSL failures
_DEFAULT_RETRYABLE_EXCEPTIONS = (
    ClientError,
    httpx.ConnectError,
    httpx.ReadError,
    ConnectionError,
    OSError,
)


def retry_with_exponential_backoff(
    max_retries: int = 5,
    initial_delay: float = 1.0,
    exponential_base: float = 5.0,
    max_delay: float = 60.0,
    exceptions: tuple = _DEFAULT_RETRYABLE_EXCEPTIONS,
):
    """Retry a function with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts (default: 5)
        initial_delay: Initial delay in seconds (default: 1.0)
        exponential_base: Base for exponential backoff (default: 5.0)
                         Delays: 1s, 5s, 25s, 60s (capped), 60s
        max_delay: Maximum delay between retries in seconds (default: 60.0)
        exceptions: Tuple of exception types to catch (default: (ClientError,))

    Returns:
        Decorated function that retries on failure with exponential backoff

    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            retry_num = 0
            while retry_num <= max_retries:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    # Never retry permission/auth/not-found errors
                    if hasattr(e, "code") and e.code in (401, 403, 404):
                        msg = str(e)
                        if e.code == 404 and "image-segmentation-001" in msg:
                            logger.error(
                                f"{func.__name__}: Model not enabled. Enable it at "
                                f"https://console.cloud.google.com/vertex-ai/publishers/google/model-garden/image-segmentation-001"
                            )
                        logger.error(
                            f"{func.__name__} failed with non-retryable error ({e.code}): {e}"
                        )
                        raise

                    # 429 rate limit: always retry without counting toward attempt limit
                    is_rate_limit = hasattr(e, "code") and e.code == 429
                    if is_rate_limit:
                        delay = min(
                            initial_delay * (exponential_base**retry_num),
                            max_delay,
                        )
                        logger.warning(
                            f"{func.__name__} rate limited (429), retrying in {delay:.1f}s (not counting as attempt)"
                        )
                        time.sleep(delay)
                        continue

                    if retry_num == max_retries:
                        logger.error(
                            f"{func.__name__} failed after {max_retries} retries: {e}"
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(
                        initial_delay * (exponential_base**retry_num), max_delay
                    )
                    retry_num += 1

                    logger.warning(
                        f"{func.__name__} failed (attempt {retry_num}/{max_retries + 1}), retrying in {delay:.1f}s: {e}"
                    )
                    time.sleep(delay)

            return None

        return wrapper

    return decorator


def get_mime_type_from_bytes(data):
    """Detect MIME type from file signature (magic bytes).

    Args:
        data: Bytes to analyze

    Returns:
        str: Detected MIME type (e.g., "image/png", "video/mp4", "image/jpeg")

    """
    if len(data) < 12:
        return "application/octet-stream"

    # PNG signature
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"

    # JPEG signature
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"

    # WebP signature
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    # GIF signature
    if data[:3] == b"GIF":
        return "image/gif"

    # AVIF signature (HEIF container with avif brand)
    # Structure: [4 bytes size][ftyp][brand]
    if data[4:8] == b"ftyp" and (
        data[8:12] == b"avif" or b"avif" in data[8:32]
    ):
        return "image/avif"

    # MP4/MOV signatures
    if b"ftyp" in data[4:12]:
        return "video/mp4"

    # WebM signature
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"

    # AVI signature
    if data[:4] == b"RIFF" and b"AVI " in data[:16]:
        return "video/avi"

    return "application/octet-stream"


def get_mime_type_from_path(path):
    """Detect MIME type from file extension in path.

    Args:
        path: File path or URL

    Returns:
        str: Detected MIME type

    """
    path_lower = path.lower()

    # Image formats
    if path_lower.endswith(".png"):
        return "image/png"
    elif path_lower.endswith(".webp"):
        return "image/webp"
    elif path_lower.endswith(".gif"):
        return "image/gif"
    elif path_lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    elif path_lower.endswith(".avif"):
        return "image/avif"

    # Video formats
    elif path_lower.endswith(".mp4"):
        return "video/mp4"
    elif path_lower.endswith(".webm"):
        return "video/webm"
    elif path_lower.endswith(".avi"):
        return "video/avi"
    elif path_lower.endswith(".mov"):
        return "video/mp4"  # MOV uses mp4 container

    # Default to JPEG for images
    return "image/jpeg"


def get_part(input_piece, return_dict=False, video_metadata=None):
    """Convert input to appropriate Part type (text, image/video bytes, or GCS path).

    Auto-detects MIME type from bytes or file extension.

    Args:
        input_piece: Can be a string (text or GCS path) or bytes (image/video)
        return_dict: If True, returns the part as a JSON dict
        video_metadata: Optional VideoMetadata (e.g. types.VideoMetadata(fps=2)).
                        If provided, input_piece is treated as video bytes.

    Returns:
        Part object or dict representation

    """
    if isinstance(input_piece, bytes):
        mime_type = get_mime_type_from_bytes(input_piece)
        if video_metadata is not None:
            part = types.Part(
                inline_data=types.Blob(data=input_piece, mime_type=mime_type),
                video_metadata=video_metadata,
            )
        else:
            part = types.Part.from_bytes(data=input_piece, mime_type=mime_type)
    elif isinstance(input_piece, str) and "gs://" in input_piece:
        mime_type = get_mime_type_from_path(input_piece)
        part = types.Part.from_uri(file_uri=input_piece, mime_type=mime_type)
    else:
        part = types.Part.from_text(text=input_piece)

    if return_dict:
        return part.to_json_dict()
    return part


def get_generate_content_config(
    temperature: float = 1,
    top_p: float = 0.95,
    max_output_tokens: int = 32768,
    response_modalities: list[str] | None = None,
    response_mime_type: str | None = None,
    response_schema: dict | None = None,
    system_instruction: str | None = None,
    thinking_budget: int | None = None,
    thinking_level: str | None = None,
    safety_off: bool = True,
    image_config: dict | None = None,
) -> types.GenerateContentConfig:
    """Create standard configuration for Gemini content generation.

    Args:
        temperature: Temperature for generation (default: 1)
        top_p: Top-p sampling parameter (default: 0.95)
        max_output_tokens: Maximum tokens to generate (default: 32768)
        response_modalities: List of response types (default: None/empty)
                           Examples: ["IMAGE", "TEXT"], ["TEXT"], ["IMAGE"]
        response_mime_type: MIME type for response (e.g., "application/json", "text/plain")
        response_schema: Schema for structured output (for JSON responses)
        system_instruction: System prompt/instruction text (default: None)
        thinking_budget: Token budget for thinking/reasoning (default: None) — DEPRECATED, use thinking_level
        thinking_level: Thinking level string e.g. "HIGH", "MEDIUM", "LOW" (default: None)
        safety_off: If True, disables all safety settings (default: True)
        image_config: Dict with image generation settings (default: None)
                     Keys: aspect_ratio, image_size, output_mime_type
                     Example: {"aspect_ratio": "3:4", "image_size": "1K", "output_mime_type": "image/png"}

    Returns:
        GenerateContentConfig object

    """
    config_params = {
        "temperature": temperature,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
    }

    if response_modalities is not None:
        config_params["response_modalities"] = response_modalities

    if response_mime_type is not None:
        config_params["response_mime_type"] = response_mime_type

    if response_schema is not None:
        config_params["response_schema"] = response_schema

    if system_instruction is not None:
        config_params["system_instruction"] = [
            types.Part.from_text(text=system_instruction)
        ]

    if thinking_level is not None:
        config_params["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_level
        )
    elif thinking_budget is not None:
        config_params["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget
        )

    if image_config is not None:
        config_params["image_config"] = types.ImageConfig(**image_config)

    if safety_off:
        config_params["safety_settings"] = [
            types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT", threshold="OFF"
            ),
        ]

    return types.GenerateContentConfig(**config_params)
