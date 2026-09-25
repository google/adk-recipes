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

import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai.types import Part

logger = logging.getLogger(__name__)

__all__ = ["save_artifact"]


async def save_artifact(
    tool_context: ToolContext,
    content: str,
    filename: str,
    format: str = "markdown",
) -> dict[str, Any]:
    """Saves text content as an ADK artifact.

    This tool takes text input and saves it as an artifact using
    the configured ArtifactService. The artifact will be versioned automatically.

    Args:
        tool_context (ToolContext): The ADK tool context providing access to
          artifact service methods.
        content (str): The text content to save as an artifact.
        filename (str): The name for the artifact file. The agent should choose
          a descriptive name.
        format (str): The format of the content. Currently supported:
          'markdown'. Defaults to 'markdown'.

    Returns:
        dict[str, Any]: A dictionary containing:
            - status (str): 'success' or 'error'
            - filename (str): The name of the created artifact
            - version (int): The version number assigned to the artifact (on
              success)
            - message (str): A descriptive message about the operation result
            - error (str, optional): Error details if the operation failed
    """
    try:
        if not content or not filename:
            return {
                "status": "error",
                "filename": filename,
                "message": "Content and filename needs to be provided",
                "error": "Missing required parameters",
            }

        if format.lower() == "markdown":
            mime_type = "text/markdown"
            if not filename.lower().endswith(".md"):
                filename = f"{filename}.md"
        else:
            mime_type = "text/plain"

        content_bytes = content.encode("utf-8")

        artifact = Part.from_bytes(data=content_bytes, mime_type=mime_type)

        try:
            version = await tool_context.save_artifact(
                filename=filename, artifact=artifact
            )
        except ValueError as e:
            logger.error(f"ValueError while saving artifact: {e!s}")
            return {
                "status": "error",
                "filename": filename,
                "message": (
                    "ArtifactService not configured. Ensure artifact_service is provided to the Runner."
                ),
                "error": str(e),
            }

        logger.info(
            f"Successfully saved artifact '{filename}' as version {version}"
        )

        return {
            "status": "success",
            "filename": filename,
            "version": version,
            "message": (
                f"Successfully saved content to artifact '{filename}' (version {version})"
            ),
        }

    except Exception as e:
        logger.error(f"Unexpected error: {e!s}")
        return {
            "status": "error",
            "filename": filename,
            "message": (
                "An unexpected error occurred while saving the artifact"
            ),
            "error": str(e),
        }
