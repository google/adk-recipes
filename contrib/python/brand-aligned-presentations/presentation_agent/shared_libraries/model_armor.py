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

import os

import google.auth
import httpx
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.auth.transport.requests import Request as AuthRequest
from google.genai import types

from presentation_agent.shared_libraries.config import (
    MODEL_ARMOR_TEMPLATE_ID,
    get_logger,
)

logger = get_logger("model_armor")


async def _call_model_armor_api(
    endpoint_suffix: str, payload: dict
) -> dict | None:
    """Helper to call Model Armor REST API."""
    try:
        # 1. Obtain Application Default Credentials
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        credentials, default_project_id = google.auth.default(scopes=scopes)
        credentials.refresh(AuthRequest())

        # 2. Build the Model Armor REST URL
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or default_project_id

        if not project:
            logger.warning("Project ID not found. Skipping Model Armor.")
            return None

        # MODEL_ARMOR_TEMPLATE_ID is already a full resource path
        # e.g., projects/{project}/locations/{location}/templates/{template_id}
        base_url = (
            f"https://modelarmor.googleapis.com/v1/{MODEL_ARMOR_TEMPLATE_ID}"
        )
        url = f"{base_url}:{endpoint_suffix}"

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
            "x-goog-user-project": project,
        }

        # 3. Execute the Async HTTP Request
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, headers=headers, json=payload, timeout=10.0
            )

        response.raise_for_status()
        return response.json()

    except Exception as e:
        logger.error(
            f"Error calling Model Armor REST API ({endpoint_suffix}): {e}"
        )
        return None


async def model_armor_interceptor(
    callback_context: CallbackContext,
) -> types.Content | None:
    """
    ADK Agent Callback that intercepts user input before the agent runs.
    """
    if not MODEL_ARMOR_TEMPLATE_ID:
        return None

    user_content = callback_context.user_content
    if not user_content or not user_content.parts:
        return None

    user_text = " ".join(p.text for p in user_content.parts if p.text)
    if not user_text:
        return None

    logger.info(
        f"Checking Prompt with Model Armor... Template: {MODEL_ARMOR_TEMPLATE_ID}"
    )

    # Use camelCase for v1 REST API payload
    payload = {"userPromptData": {"text": user_text}}
    data = await _call_model_armor_api("sanitizeUserPrompt", payload)
    if data:
        logger.info(f"DEBUG: Full Model Armor Response: {data}")

    if not data:
        return types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text="Security Verification Error. Unable to process prompt."
                )
            ],
        )

    sanitization_result = data.get("sanitizationResult", {})
    verdict = sanitization_result.get("sanitizationVerdict")

    if verdict == "MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK":
        reason = sanitization_result.get(
            "sanitizationVerdictReason", "Policy Violation"
        )
        logger.warning(f"Model Armor BLOCKED prompt. Reason: {reason}")
        return types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=(
                        "I cannot fulfill this request as it violates "
                        "the enterprise AI security policy."
                    )
                )
            ],
        )

    return None


async def model_armor_response_interceptor(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """
    ADK Agent Callback that intercepts model output after the LLM runs.
    """
    if not MODEL_ARMOR_TEMPLATE_ID:
        return None

    if not llm_response.content or not llm_response.content.parts:
        return None

    response_text = " ".join(
        p.text for p in llm_response.content.parts if p.text
    )
    if not response_text:
        return None

    logger.info(
        f"Checking Response with Model Armor... Template: {MODEL_ARMOR_TEMPLATE_ID}"
    )

    payload = {"modelResponseData": {"text": response_text}}
    data = await _call_model_armor_api("sanitizeModelResponse", payload)

    if not data:
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Security Verification Error. Unable to process response."
                    )
                ],
            )
        )

    sanitization_result = data.get("sanitizationResult", {})
    verdict = sanitization_result.get("sanitizationVerdict")

    if verdict == "MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK":
        reason = sanitization_result.get(
            "sanitizationVerdictReason", "Policy Violation"
        )
        logger.warning(f"Model Armor BLOCKED response. Reason: {reason}")
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=(
                            "The generated response was blocked by enterprise security policy."
                        )
                    )
                ],
            )
        )

    # Check for SDP Redaction
    filter_match_results = sanitization_result.get("filterMatchResults", [])
    redacted_text = response_text
    sdp_found = False

    for result in filter_match_results:
        sdp_result = result.get("sdpFilterMatchResult")
        if sdp_result and sdp_result.get("redactedContent"):
            redacted_text = sdp_result.get("redactedContent")
            sdp_found = True

    if sdp_found:
        logger.info("Model Armor REDACTED sensitive data in response.")
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=redacted_text)],
            )
        )

    return None
