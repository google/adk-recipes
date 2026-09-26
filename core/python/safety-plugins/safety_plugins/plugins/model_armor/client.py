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

"""Google Model Armor client initialization and RPC dispatch."""

from google.api_core.client_options import ClientOptions
from google.auth.credentials import Credentials
from google.cloud import modelarmor_v1

from .constants import (
    ModelArmorMethod,
    ModelArmorResponse,
)
from .logs import log_client_initialized


class UnsupportedModelArmorMethodError(ValueError):
    """A caller requested an unsupported Model Armor RPC."""

    def __init__(self, method: str) -> None:
        self.method = method
        super().__init__(f"Unsupported Model Armor method: {method}")


class ModelArmorClient:
    """Build Model Armor requests and lazily initialize the Google client."""

    def __init__(
        self,
        *,
        project_id: str,
        location_id: str,
        template_id: str,
        credentials: Credentials | None = None,
    ) -> None:
        if not project_id or not template_id:
            raise ValueError("Model Armor requires a project and template ID")
        if not location_id or location_id == "global":
            raise ValueError(
                "Model Armor requires a regional location, not 'global'; "
                "set GOOGLE_CLOUD_MODEL_ARMOR_LOCATION"
            )
        self._project_id = project_id
        self._credentials = credentials
        self._location_id = location_id
        self._template_name = f"projects/{project_id}/locations/{location_id}/templates/{template_id}"
        self._client_instance: modelarmor_v1.ModelArmorAsyncClient | None = None

    @property
    def _client(self) -> modelarmor_v1.ModelArmorAsyncClient:
        if self._client_instance is None:
            self._client_instance = modelarmor_v1.ModelArmorAsyncClient(
                credentials=self._credentials,
                client_options=ClientOptions(
                    api_endpoint=f"modelarmor.{self._location_id}.rep.googleapis.com"
                ),
            )
            log_client_initialized(
                project_id=self._project_id,
                location_id=self._location_id,
            )
        return self._client_instance

    async def close(self) -> None:
        """Close the Google transport if the client was initialized."""
        if self._client_instance is None:
            return
        await self._client_instance.transport.close()
        self._client_instance = None

    async def sanitize(
        self,
        method: ModelArmorMethod,
        text: str,
        *,
        timeout_s: float,
    ) -> ModelArmorResponse:
        """Dispatch to the RPC named by ``method``.

        The transport deadline bounds the real RPC. Automatic retries are
        disabled because Model Armor is on the request's critical path and the
        application already applies its own availability policy.

        An unknown method raises rather than falling back to either RPC:
        screening a user prompt as a model response would silently skip the
        prompt-injection detector.
        """
        data = modelarmor_v1.DataItem(text=text)
        match method:
            case ModelArmorMethod.SANITIZE_USER_PROMPT:
                return await self._client.sanitize_user_prompt(
                    request=modelarmor_v1.SanitizeUserPromptRequest(
                        name=self._template_name,
                        user_prompt_data=data,
                    ),
                    timeout=timeout_s,
                    retry=None,
                )
            case ModelArmorMethod.SANITIZE_MODEL_RESPONSE:
                return await self._client.sanitize_model_response(
                    request=modelarmor_v1.SanitizeModelResponseRequest(
                        name=self._template_name,
                        model_response_data=data,
                    ),
                    timeout=timeout_s,
                    retry=None,
                )
            case _:
                raise UnsupportedModelArmorMethodError(method)
