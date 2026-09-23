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

"""
Before-agent callback for the Orchestrator Agent.

Extracts the loan_request_id from the user's message and handles inline
document storage for ADK evaluations.
"""

import base64

from google.adk.agents.callback_context import CallbackContext
from google.genai import types

from small_business_loan_agent.shared_libraries.logging_config import get_logger
from small_business_loan_agent.shared_libraries.request_id_utils import (
    extract_request_id_from_text,
)

logger = get_logger(__name__)


def _extract_inline_document_from_user_content(
    callback_context: CallbackContext,
) -> dict | None:
    """Extract inline document from user content for storage in session state."""
    try:
        user_content = callback_context._invocation_context.user_content
        if not user_content or not user_content.parts:
            return None

        document_mime_types = [
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/gif",
            "image/webp",
            "image/tiff",
        ]

        for i, part in enumerate(user_content.parts):
            if hasattr(part, "inline_data") and part.inline_data:
                inline_data = part.inline_data
                mime_type = getattr(inline_data, "mime_type", None)
                data = getattr(inline_data, "data", None)

                if mime_type in document_mime_types and data:
                    if isinstance(data, bytes):
                        data_str = base64.b64encode(data).decode("utf-8")
                    else:
                        data_str = data

                    ext_map = {
                        "application/pdf": ".pdf",
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/jpg": ".jpg",
                    }
                    ext = ext_map.get(mime_type, ".bin")
                    filename = f"inline_document_{i}{ext}"

                    return {
                        "mime_type": mime_type,
                        "data": data_str,
                        "filename": filename,
                    }

        return None

    except Exception as e:
        logger.warning(f"Error extracting inline document: {e}")
        return None


async def _check_artifacts_available(callback_context: CallbackContext) -> bool:
    """Check if artifacts are available in the artifact service."""
    try:
        artifact_service = callback_context._invocation_context.artifact_service
        if artifact_service is None:
            return False

        cur_session = callback_context._invocation_context.session
        available_files = await artifact_service.list_artifact_keys(
            app_name=cur_session.app_name,
            user_id=cur_session.user_id,
            session_id=cur_session.id,
        )

        return bool(available_files)

    except Exception:
        return False


async def _store_inline_document_if_needed(
    callback_context: CallbackContext,
) -> None:
    """Store inline documents in session state if no artifacts are available."""
    artifacts_available = await _check_artifacts_available(callback_context)

    if artifacts_available:
        return

    inline_doc = _extract_inline_document_from_user_content(callback_context)
    if inline_doc:
        callback_context.state["inline_document"] = inline_doc
        logger.info(
            f"Stored inline document in session state: {inline_doc['filename']}"
        )


async def extract_request_id_from_request(
    callback_context: CallbackContext,
) -> types.Content | None:
    """
    Before-agent callback to extract loan_request_id from user message.

    Returns None to allow the agent to proceed, or types.Content with a helpful message
    if no request ID is found.
    """
    try:
        if callback_context.state.get("loan_request_id"):
            return None

        user_content = callback_context._invocation_context.user_content
        if not user_content or not user_content.parts:
            return None

        user_message = None
        for part in user_content.parts:
            if hasattr(part, "text") and part.text:
                user_message = part.text
                break

        if not user_message:
            return None

        try:
            request_id = extract_request_id_from_text(user_message)
            callback_context.state["loan_request_id"] = request_id
            logger.info(f"Extracted and stored loan_request_id: {request_id}")

            await _store_inline_document_if_needed(callback_context)
            return None

        except ValueError:
            return types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            "I need a loan request ID to process your request.\n\n"
                            "Please provide your message in one of these formats:\n\n"
                            "**For new application processing:**\n"
                            '"Process this application for SBL-2025-00142"\n'
                            "(Then upload your document)\n\n"
                            "**To check status:**\n"
                            '"What is the status on SBL-2025-00142?"\n\n'
                            "The request ID format is: SBL-YYYY-XXXXX (e.g., SBL-2025-00142)"
                        )
                    )
                ],
            )

    except Exception as e:
        logger.exception(f"Error in extract_request_id_from_request: {e}")
        return types.Content(
            role="model",
            parts=[
                types.Part(
                    text="An unexpected error occurred while processing your request. Please try again later."
                )
            ],
        )
