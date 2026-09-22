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

"""Gemini text generation and embedding helpers.

Retry logic is handled by the SDK client (configured in src.utils.config
with HttpRetryOptions), not here.
"""

from __future__ import annotations

from google.genai import types

from src.utils.llm_utils import get_generate_content_config, get_part


def generate_gemini(
    content_pieces: list,
    client,
    schema=None,
    config: types.GenerateContentConfig | None = None,
    model: str | None = None,
    video_metadata: types.VideoMetadata | None = None,
) -> str:
    """Generate text content using Gemini.

    Args:
        content_pieces: List of text strings, image/doc bytes, or GCS paths.
        client: Gemini client instance.
        schema: Optional response schema for structured output.
        config: Optional GenerateContentConfig (uses default if None).
        model: Model to use. Defaults to the configured agent model
            (MODEL_NAME) when None — model identifiers are read from the
            environment, never hardcoded in source.
        video_metadata: Optional VideoMetadata applied to bytes parts.

    Returns:
        Generated text response (stripped).
    """
    if model is None:
        # Deferred import: src.utils.config imports nothing from this module,
        # but resolving it at module scope would rebuild the config dataclass
        # during import of the gemini helpers.
        from src.utils.config import config as pipeline_config

        model = pipeline_config.agent_gemini_model

    parts = [get_part(x, video_metadata=video_metadata) for x in content_pieces]
    contents = [types.Content(role="user", parts=parts)]

    if config is None:
        config = get_generate_content_config(
            temperature=0,
            response_modalities=["TEXT"],
            response_mime_type="application/json" if schema else "text/plain",
            response_schema=schema,
        )

    result = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    if not result.candidates:
        # Empty candidates usually means the response was blocked by the
        # safety/content filter or hit the token limit before producing
        # any text. Surface a clearer error than the generic NoneType
        # subscript so the failure is diagnosable in BQ logs.
        finish_reason = getattr(
            getattr(result, "prompt_feedback", None), "block_reason", None
        )
        raise ValueError(
            f"Gemini returned no candidates (block_reason={finish_reason})"
        )
    resp_parts = result.candidates[0].content.parts
    if not resp_parts or resp_parts[0].text is None:
        return ""
    return resp_parts[0].text.strip()
