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

"""Callbacks for the Document Extraction Agent."""

import asyncio
import base64
import os

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.cloud import storage
from google.genai import types

from small_business_loan_agent.shared_libraries.logging_config import get_logger

logger = get_logger(__name__)


def get_gcs_data_bucket() -> str:
    """Return the GCS data bucket name, or an empty string if unconfigured.

    Falls back to a conventional `<project>-small-business-loan-data` bucket
    when `GCS_DATA_BUCKET` is unset but a project is known. Returns "" when
    neither is configured so callers can detect the unconfigured case.
    """
    bucket = os.getenv("GCS_DATA_BUCKET")
    if bucket:
        return bucket
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        return ""
    return f"{project}-small-business-loan-data"


async def _load_document_from_artifacts(
    callback_context: CallbackContext,
) -> tuple[bytes | None, str]:
    """Load uploaded document from the artifact service.

    In Gemini Enterprise (AgentSpace), uploaded files are stored as artifacts
    by the platform. This function retrieves the file bytes, handling both
    ADK Web format (Part object) and AgentSpace format (dict with inlineData).

    Returns:
        Tuple of (file_bytes, mime_type). Returns (None, "") if not found.
    """
    try:
        artifact_service = callback_context._invocation_context.artifact_service
        if artifact_service is None:
            return None, ""

        cur_session = callback_context._invocation_context.session
        app_name = cur_session.app_name
        user_id = cur_session.user_id
        session_id = cur_session.id

        available_files = await artifact_service.list_artifact_keys(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

        if not available_files:
            return None, ""

        artifact_name = available_files[0]
        artifact_data = await artifact_service.load_artifact(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            filename=artifact_name,
        )

        # AgentSpace format (dictionary with inlineData)
        if (
            artifact_data
            and isinstance(artifact_data, dict)
            and "inlineData" in artifact_data
        ):
            inline_data = artifact_data["inlineData"]
            mime = inline_data.get("mimeType", "application/pdf")
            file_data = inline_data.get("data", "")
            if isinstance(file_data, str):
                return base64.b64decode(file_data), mime
            return file_data, mime

        # ADK Web format (Part object with inline_data attribute)
        if (
            artifact_data
            and hasattr(artifact_data, "inline_data")
            and artifact_data.inline_data
        ):
            mime = getattr(
                artifact_data.inline_data, "mime_type", "application/pdf"
            )
            return artifact_data.inline_data.data, mime

        # Unknown format
        logger.warning(
            f"Unexpected artifact format: {type(artifact_data).__name__}"
        )

    except Exception as e:
        logger.error(f"Error loading document from artifact service: {e}")

    return None, ""


async def _load_document_from_gcs(bucket_name: str, blob_name: str) -> bytes:
    """Download a file from GCS and return bytes."""

    # Run in thread executor as storage client is synchronous
    def _download():
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        return blob.download_as_bytes()

    return await asyncio.to_thread(_download)


def _get_inline_doc(state: dict) -> types.Part | None:
    """Helper to extract document from session state."""
    inline_doc = state.get("inline_document")
    if inline_doc and isinstance(inline_doc, dict):
        data_b64 = inline_doc.get("data", "")
        mime_type = inline_doc.get("mime_type", "application/pdf")
        if data_b64:
            try:
                raw_bytes = base64.b64decode(data_b64)
                logger.info(
                    f"Loaded document from session state: mime_type={mime_type}, size={len(raw_bytes)} bytes"
                )
                return types.Part(
                    inline_data=types.Blob(mime_type=mime_type, data=raw_bytes)
                )
            except Exception as e:
                logger.error(f"Failed to decode inline_document base64: {e}")
    return None


async def _get_artifact_doc(
    callback_context: CallbackContext,
) -> types.Part | None:
    """Helper to extract document from artifact service."""
    raw_bytes, mime_type = await _load_document_from_artifacts(callback_context)
    if raw_bytes:
        if not isinstance(raw_bytes, bytes):
            if isinstance(raw_bytes, str):
                raw_bytes = raw_bytes.encode("utf-8")
            else:
                raw_bytes = bytes(raw_bytes)

        logger.info(
            f"Loaded document from artifact service: mime_type={mime_type}, size={len(raw_bytes)} bytes"
        )
        return types.Part(
            inline_data=types.Blob(mime_type=mime_type, data=raw_bytes)
        )
    return None


async def _get_gcs_doc(user_message: str) -> types.Part | None:
    """Helper to extract document from GCS fallback."""
    gcs_bucket = get_gcs_data_bucket()
    if gcs_bucket:
        trigger_keywords = [
            "gcs",
            "sample_application_complete.pdf",
            "sample_application_incomplete.pdf",
        ]
        if any(kw.lower() in user_message.lower() for kw in trigger_keywords):
            logger.info(
                "Triggered GCS file fetch based on user request keywords."
            )
            file_to_fetch = "sample_application_complete.pdf"  # Default
            if "sample_application_incomplete.pdf" in user_message:
                file_to_fetch = "sample_application_incomplete.pdf"
            try:
                raw_bytes = await _load_document_from_gcs(
                    gcs_bucket, file_to_fetch
                )
                mime_type = "application/pdf"
                logger.info(
                    f"Loaded document from GCS: {file_to_fetch}, size={len(raw_bytes)} bytes"
                )
                return types.Part(
                    inline_data=types.Blob(mime_type=mime_type, data=raw_bytes)
                )
            except Exception as e:
                logger.error(f"Failed to load document from GCS: {e}")
    else:
        logger.warning(
            "No document found and GCS_DATA_BUCKET not configured for fallback."
        )
    return None


async def inject_document_into_request(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> None:
    """Before-model callback that injects the document into the LLM request.

    When the DocumentExtractionAgent is called as a sub-agent via AgentTool,
    the original multimodal content (PDF/image) is not forwarded. This callback
    retrieves the document and injects it so Gemini can natively process it.

    Document sources (checked in order):
    1. Session state "inline_document" — file sent as inline_data
    2. Artifact service — file uploaded through Gemini Enterprise (AgentSpace),
    3. GCS fallback — if enabled by presence of keywords in user request
    """
    doc_part = _get_inline_doc(callback_context.state)

    if doc_part is None:
        doc_part = await _get_artifact_doc(callback_context)

    if doc_part is None:
        user_message = ""
        for content in llm_request.contents:
            if content.role == "user":
                for part in content.parts:
                    if hasattr(part, "text") and part.text:
                        user_message += part.text + " "

        doc_part = await _get_gcs_doc(user_message)

    if doc_part is None:
        logger.warning(
            "No document found in session state, artifact service, or GCS"
        )
        return None

    llm_request.contents.append(
        types.Content(
            role="user",
            parts=[
                types.Part(
                    text="Here is the loan application document to extract data from:"
                ),
                doc_part,
            ],
        )
    )

    logger.info("Injected document into LLM request")
    return None
