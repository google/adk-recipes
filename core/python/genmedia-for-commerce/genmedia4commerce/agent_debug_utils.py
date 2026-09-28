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

"""Debug utilities for the GenMedia ADK Agent.

These functions dump LLM requests and artifacts to GCS for debugging.
Not imported by default — use manually when needed.

Usage:
    from genmedia4commerce.agent_debug_utils import dump_llm_request, dump_all_artifacts
    dump_llm_request(llm_request, global_session_id, media_bucket)
    await dump_all_artifacts(callback_context, global_session_id, media_bucket)
"""

import json
import logging
from datetime import UTC, datetime

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest

logger = logging.getLogger(__name__)


def dump_llm_request(
    llm_request: LlmRequest, global_session_id: str, media_bucket: str
):
    """Serialize llm_request to GCS for debugging."""
    try:
        from google.cloud import storage

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        blob_path = f"sessions/{global_session_id}/debug_llm_requests/{ts}.json"

        data = {}
        if llm_request.contents:
            serialized = []
            for content in llm_request.contents:
                parts = []
                for part in content.parts or []:
                    if part.text:
                        parts.append({"text": part.text})
                    elif part.inline_data and part.inline_data.data:
                        parts.append(
                            {
                                "inline_data": f"[{part.inline_data.mime_type}, {len(part.inline_data.data)} bytes]"
                            }
                        )
                    elif part.function_call:
                        parts.append(
                            {
                                "function_call": {
                                    "name": part.function_call.name,
                                    "args": dict(part.function_call.args)
                                    if part.function_call.args
                                    else {},
                                }
                            }
                        )
                    elif part.function_response:
                        parts.append(
                            {
                                "function_response": {
                                    "name": part.function_response.name
                                }
                            }
                        )
                    else:
                        parts.append({"unknown": str(type(part))})
                serialized.append({"role": content.role, "parts": parts})
            data["contents"] = serialized

        if llm_request.config:
            data["config"] = str(llm_request.config)

        client = storage.Client()
        bucket = client.bucket(media_bucket)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(data, indent=2, default=str),
            content_type="application/json",
        )
        logger.info(
            f"[Debug] Dumped llm_request to gs://{media_bucket}/{blob_path}"
        )
    except Exception as e:
        logger.warning(f"[Debug] Failed to dump llm_request: {e}")


async def dump_all_artifacts(
    callback_context: CallbackContext, global_session_id: str, media_bucket: str
):
    """Save all artifacts from the artifact service to GCS debug_artifacts/ in order."""
    try:
        invocation_context = callback_context._invocation_context
        artifact_service = invocation_context.artifact_service
        app_name = invocation_context.app_name
        user_id = invocation_context.user_id

        keys = await artifact_service.list_artifact_keys(
            app_name=app_name, user_id=user_id, session_id=global_session_id
        )
        logger.debug(f"[Debug] All artifact keys ({len(keys)}): {keys}")

        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(media_bucket)

        for i, key in enumerate(keys):
            artifact = await callback_context.load_artifact(filename=key)
            if artifact and artifact.inline_data and artifact.inline_data.data:
                blob_path = (
                    f"sessions/{global_session_id}/debug_artifacts/{i}_{key}"
                )
                blob = bucket.blob(blob_path)
                blob.upload_from_string(
                    artifact.inline_data.data,
                    content_type=artifact.inline_data.mime_type or "image/jpeg",
                )
                logger.debug(
                    f"[Debug] Saved artifact {i}: {key} → gs://{media_bucket}/{blob_path}"
                )
            else:
                logger.debug(f"[Debug] Artifact {i}: {key} — no inline_data")
    except Exception as e:
        logger.warning(f"[Debug] Failed to dump artifacts: {e}")
