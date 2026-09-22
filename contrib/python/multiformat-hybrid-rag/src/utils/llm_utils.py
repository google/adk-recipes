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

"""Shared LLM utilities for Gemini content generation.

Retry logic is handled by the SDK client (configured with
HttpRetryOptions in src.utils.config), not here.
"""

import logging

from google.genai import types

logger = logging.getLogger(__name__)


def get_mime_type_from_bytes(data: bytes) -> str:
    """Detect MIME type from file signature (magic bytes).

    Needs at least 32 bytes to check every signature reliably; shorter
    inputs are treated as opaque rather than slicing past the end.
    """
    if len(data) < 32:
        return "application/octet-stream"

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[4:8] == b"ftyp" and (
        data[8:12] == b"avif" or b"avif" in data[8:32]
    ):
        return "image/avif"
    if b"ftyp" in data[4:12]:
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if data[:4] == b"RIFF" and b"AVI " in data[:16]:
        return "video/avi"
    if data[:4] == b"%PDF":
        return "application/pdf"

    return "application/octet-stream"


def get_mime_type_from_path(path: str) -> str:
    """Detect MIME type from file extension."""
    path_lower = path.lower()

    ext_map = {
        # Images
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".avif": "image/avif",
        ".bmp": "image/bmp",
        # Video
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".avi": "video/avi",
        ".mov": "video/mp4",
        ".wmv": "video/x-ms-wmv",
        ".mpg": "video/mpeg",
        # Audio
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        # Documents
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".rtf": "application/rtf",
        # Text
        ".html": "text/html",
        ".htm": "text/html",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".jsonl": "application/jsonl",
    }

    for ext, mime in ext_map.items():
        if path_lower.endswith(ext):
            return mime

    return "application/octet-stream"


def get_part(
    input_piece: str | bytes,
    return_dict: bool = False,
    video_metadata: types.VideoMetadata | None = None,
) -> types.Part:
    """Convert input to appropriate Gemini Part type.

    Handles text, image/video bytes, and GCS paths. Auto-detects MIME type.
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
) -> types.GenerateContentConfig:
    """Create standard configuration for Gemini content generation."""
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
