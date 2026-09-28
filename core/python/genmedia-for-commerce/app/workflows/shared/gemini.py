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

"""Shared utilities for Gemini text generation and embeddings."""

# Standard library imports
import os

# Third-party imports
import numpy as np
from google.genai import types

# Project imports
from workflows.shared.llm_utils import (
    get_generate_content_config,
    get_part,
    retry_with_exponential_backoff,
)


@retry_with_exponential_backoff(max_retries=5)
def generate_gemini(
    text_images_pieces,
    client,
    schema=None,
    config=None,
    model=os.getenv("MODEL_NAME_GENERATED_1"),  # noqa: B008
    video_metadata=None,
):
    """Generate text content using Gemini.

    Args:
        text_images_pieces: List of text strings, image bytes, or GCS paths
        client: Gemini client instance
        schema: Optional response schema for structured output
        config: Optional GenerateContentConfig (uses default if None)
        model: Model to use (default: "gemini-3.6-flash")
        video_metadata: Optional VideoMetadata applied to bytes parts
                        (e.g. types.VideoMetadata(fps=2))

    Returns:
        str: Generated text response (stripped)

    """
    parts = [
        get_part(x, video_metadata=video_metadata) for x in text_images_pieces
    ]
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
    return result.candidates[0].content.parts[0].text.strip()


@retry_with_exponential_backoff(max_retries=5)
def embed_gemini(
    content_pieces,
    client,
    model=None,
):
    """Generate embeddings using Gemini embedding models.

    Args:
        content_pieces: List of text strings, image bytes, or GCS paths
                        (same format as generate_gemini's text_images_pieces)
        client: Gemini client instance
        model: Embedding model to use (default from MULTIMODAL_EMBEDDING_MODEL env var)

    Returns:
        np.ndarray: Embedding vector as float32 numpy array

    """
    if model is None:
        model = os.getenv(
            "MULTIMODAL_EMBEDDING_MODEL", os.getenv("MODEL_NAME_GENERATED_7")
        )

    parts = [get_part(x) for x in content_pieces]
    contents = [types.Content(role="user", parts=parts)]

    result = client.models.embed_content(
        model=model,
        contents=contents,
    )
    return np.array(result.embeddings[0].values, dtype=np.float32)
