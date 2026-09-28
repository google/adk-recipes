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

import asyncio
import os
import tempfile
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import ValidationError

# --- Local Application Imports ---
from ..shared_libraries.config import (
    DEFAULT_TEMPLATE_URI,
    get_gcs_client,
    get_logger,
)
from ..shared_libraries.models import DeckSpec


async def list_available_artifacts(tool_context: ToolContext) -> list[str]:
    """Lists the filenames of all available artifacts in the session."""
    try:
        return await tool_context.list_artifacts()
    except Exception as e:
        get_logger("list_artifacts").error(f"Error listing artifacts: {e}")
        return [f"Error listing artifacts: {e}"]


async def get_artifact_as_local_path(
    tool_context: ToolContext, artifact_name: str
) -> str:
    """
    Loads a user-uploaded artifact, saves it to a local temp file, and returns the path.
    Includes a retry mechanism for cloud propagation delays.
    """
    import asyncio

    log = get_logger("get_artifact_as_local_path")
    try:
        # Attempt 1
        file_artifact = await tool_context.load_artifact(artifact_name)

        # Retry logic: Give the cloud storage a moment to sync if it's missing
        if not file_artifact:
            log.warning(
                f"Artifact '{artifact_name}' not found on first try. Waiting 2 seconds for cloud sync..."
            )
            await asyncio.sleep(2.0)
            file_artifact = await tool_context.load_artifact(artifact_name)

        if not file_artifact:
            return f"Error: Artifact '{artifact_name}' is empty or could not be loaded."

        if isinstance(file_artifact, types.Part):
            file_bytes = file_artifact.inline_data.data
        else:
            file_bytes = file_artifact

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f"_{artifact_name}"
        ) as tmp_file:
            tmp_file.write(file_bytes)
            local_path = tmp_file.name

        log.info(f"Successfully retrieved single working path: {local_path}")
        return local_path

    except Exception as e:
        log.error(
            f"Failed to load artifact '{artifact_name}': {e}", exc_info=True
        )
        return f"Error loading file: {e}"


async def get_gcs_file_as_local_path(
    gcs_uri: str | None = None,
) -> str:
    """
    Downloads a file from a specific GCS URI to a local temporary file.
    Returns the local file path if successful, otherwise an error string.
    Example GCS URI: 'gs://my-bucket-name/my-file.pptx'
    """
    log = get_logger("get_gcs_file_as_local_path")
    try:
        target_uri = gcs_uri or DEFAULT_TEMPLATE_URI
        if not target_uri or not target_uri.startswith("gs://"):
            return "Error: Invalid GCS URI. It must start with 'gs://'."

        # Parse the bucket and blob name from the URI
        path_part = target_uri[5:]
        if "/" not in path_part:
            return f"Error: Invalid GCS URI. Missing object path in '{target_uri}'."
        bucket_name, blob_name = path_part.split("/", 1)
        if not blob_name:
            return f"Error: Invalid GCS URI. Missing object path in '{target_uri}'."
        storage_client = get_gcs_client()
        if not storage_client:
            raise RuntimeError("GCS client could not be initialized.")

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        if not blob.exists():
            return f"Error: The file does not exist at the specified GCS path: {target_uri}"

        # Create a unique local file in the system's temp directory
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f"_{blob_name.split('/')[-1]}"
        ) as tmp_file:
            blob.download_to_filename(tmp_file.name)
            local_path = tmp_file.name

        log.info(
            f"Successfully downloaded default GCS file to local path: {local_path}"
        )
        return local_path

    except Exception as e:
        log.error(
            f"Failed to download default template from '{target_uri}': {e}",
            exc_info=True,
        )
        return f"Error: Could not access default GCS template. Details: {e}"


async def save_presentation(
    tool_context: ToolContext,
    new_artifact_name: str,
    local_path: str,
    gcs_bucket_name: str | None = None,
) -> str:
    """
    Saves a local presentation file to the artifact store and optionally to GCS.
    """
    log = get_logger("save_presentation")
    try:
        if not local_path or not os.path.exists(local_path):
            return f"Error: The local file at '{local_path}' does not exist."

        if not new_artifact_name.lower().endswith(".pptx"):
            new_artifact_name += ".pptx"

        # 1. Save to ADK Artifact Store
        with open(local_path, "rb") as f:
            file_bytes = f.read()
            ppt_artifact = types.Part(
                inline_data=types.Blob(
                    data=file_bytes,
                    mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                )
            )
            await tool_context.save_artifact(new_artifact_name, ppt_artifact)
        log.info(
            f"Successfully saved presentation as artifact '{new_artifact_name}'."
        )

        # 2. Optionally Backup to GCS
        gcs_message = ""
        if gcs_bucket_name:
            try:
                storage_client = get_gcs_client()
                if not storage_client:
                    raise RuntimeError("GCS client could not be initialized.")

                bucket = storage_client.bucket(gcs_bucket_name)
                blob = bucket.blob(new_artifact_name)
                # Run sync GCS upload in a thread
                await asyncio.to_thread(blob.upload_from_filename, local_path)
                gcs_message = (
                    f" It was also saved to GCS bucket '{gcs_bucket_name}'."
                )
            except Exception as e:
                log.error(f"Failed to upload to GCS: {e}")
                gcs_message = f" However, the upload to GCS failed. Error: {e}"

        return f"Successfully saved the presentation as artifact '{new_artifact_name}'.{gcs_message} The user can now download it."
    except Exception as e:
        log.error(
            f"An unexpected error occurred during save: {e}", exc_info=True
        )
        return f"Error: An unexpected error occurred during save. Details: {e}"


async def save_deck_spec(tool_context: ToolContext, deck_spec: dict) -> str:
    """
    Saves the deck_spec to persistent session STATE.
    Returns an invisible confirmation instead of a visible filename.
    """
    log = get_logger("save_deck_spec")
    try:
        # Normalize structure
        if "slide_topics" in deck_spec and "slides" not in deck_spec:
            deck_spec["slides"] = deck_spec.pop("slide_topics")

        if isinstance(deck_spec.get("slides"), dict):
            deck_spec["slides"] = list(deck_spec["slides"].values())

        if "closing_title" not in deck_spec:
            deck_spec["closing_title"] = "Thank You"

        # Validate
        validated_spec = DeckSpec(**deck_spec)

        # 1. PERSISTENCE: Save directly to ctx.state (Invisible in UI)
        tool_context.state["current_deck_spec"] = validated_spec.model_dump()

        log.info(
            "Successfully persisted deck_spec to session state (invisible to UI)."
        )
        return "Success: Presentation plan has been securely saved to the session state."
    except ValidationError as e:
        log.error(f"Validation error: {e.errors()}")
        return f"Error: Invalid deck_spec structure: {e.errors()}"
    except Exception as e:
        log.error(f"Failed to save deck_spec: {e}")
        return "Error: Internal failure while saving plan."


async def update_slide_in_spec(
    tool_context: ToolContext,
    slide_index: int,
    updated_slide_topic: dict[str, Any],
) -> str:
    """
    Surgically updates a slide in the session STATE.
    Automatically grows the deck if needed.
    """
    log = get_logger("update_slide_in_spec")
    try:
        # 1. Load from State (Primary)
        spec_dict = tool_context.state.get("current_deck_spec")

        if not spec_dict:
            return "Error: No active presentation plan found in session state. Revision failed."

        # 2. Update or Append
        slides = spec_dict.get("slides", [])

        if 0 <= slide_index < len(slides):
            slides[slide_index].update(updated_slide_topic)
        else:
            while len(slides) < slide_index:
                slides.append(
                    {
                        "title": f"New Slide {len(slides) + 1}",
                        "bullets": [],
                        "layout_name": "Title and Content",
                    }
                )

            new_slide = {
                "title": "New Slide",
                "bullets": [],
                "layout_name": "Title and Content",
            }
            new_slide.update(updated_slide_topic)
            slides.append(new_slide)

        # 3. Save back to State
        tool_context.state["current_deck_spec"] = spec_dict
        log.info(f"Slide {slide_index} updated in session state successfully.")

        return "Success: The slide has been updated in the session state."

    except Exception as e:
        log.error(f"Failed to update slide: {e}")
        return "Error: Failed to update slide copy."


async def read_file_content(
    tool_context: ToolContext, artifact_name: str
) -> str:
    """Reads the raw text content from any uploaded text-based file artifact."""
    log = get_logger("read_file_content")
    try:
        file_artifact = await tool_context.load_artifact(artifact_name)
        if not file_artifact:
            return f"Error: Artifact '{artifact_name}' is empty or could not be loaded."

        if isinstance(file_artifact, types.Part):
            file_bytes = file_artifact.inline_data.data
        else:
            file_bytes = file_artifact

        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: '{artifact_name}' is not a readable text file."
    except Exception as e:
        log.error(
            f"Failed to read artifact '{artifact_name}': {e}", exc_info=True
        )
        return f"Error reading file: {e}"


async def read_deck_spec(tool_context: ToolContext) -> dict[str, Any]:
    """
    Retrieves the current presentation plan (deck_spec) directly from session state.
    """
    log = get_logger("read_deck_spec")
    try:
        # 1. Try State (Primary Source of Truth)
        spec_dict = tool_context.state.get("current_deck_spec")

        if not spec_dict:
            return {
                "status": "Error",
                "message": "No active presentation plan found in session state. Please ensure an outline was generated.",
            }

        log.info("Successfully loaded DeckSpec from session state.")
        return {
            "status": "Success",
            "strategic_briefing": spec_dict.get("strategic_briefing"),
            "cover": spec_dict.get("cover"),
            "slides": spec_dict.get("slides", []),
            "closing_title": spec_dict.get("closing_title"),
        }
    except Exception as e:
        log.error(f"Failed to read deck_spec: {e}")
        return {
            "status": "Error",
            "message": f"Internal failure while reading plan from state: {e}",
        }
