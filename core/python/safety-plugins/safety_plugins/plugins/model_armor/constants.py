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

"""Vocabulary shared by the transport, policy and ADK adapter."""

from enum import StrEnum
from typing import TypeAlias

from google.cloud import modelarmor_v1

ModelArmorResponse: TypeAlias = (
    modelarmor_v1.SanitizeUserPromptResponse
    | modelarmor_v1.SanitizeModelResponseResponse
)


class ModelArmorMethod(StrEnum):
    SANITIZE_USER_PROMPT = "sanitizeUserPrompt"
    SANITIZE_MODEL_RESPONSE = "sanitizeModelResponse"


class ScreeningStage(StrEnum):
    USER_PROMPT = "user_prompt"
    CALLBACK_INPUT = "callback_input"
    MODEL_RESPONSE = "model_response"
    TOOL_OUTPUT = "tool_output"


class ModelArmorAction(StrEnum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


USER_PROMPT_REMOVED_MESSAGE = (
    "A safety filter has removed the last user prompt as it was deemed unsafe."
)
MODEL_RESPONSE_REMOVED_MESSAGE = (
    "A safety filter has removed the model's response as it was deemed unsafe."
)
UNSAFE_TOOL_OUTPUT_MESSAGE = "Unable to emit tool result due to unsafe outputs."
SAFETY_SERVICE_UNAVAILABLE_MESSAGE = "Safety service temporarily unavailable."
UNSAFE_PROMPT_STATE_KEY = "temp:model_armor_user_prompt_unsafe"
SDP_FILTER = "sdp"
MODEL_ARMOR_UNAVAILABLE_REASON = "model-armor-unavailable"
LOGGER_NAME = "safety_plugins.plugins.model_armor"
