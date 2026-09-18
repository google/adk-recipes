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

"""Transport and deadline tests; no Google credentials or network required."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.cloud import modelarmor_v1

from safety_plugins.plugins.model_armor import (
    ModelArmorClient,
    ModelArmorSafetyFilterPlugin,
)
from safety_plugins.plugins.model_armor.client import (
    UnsupportedModelArmorMethodError,
)
from safety_plugins.plugins.model_armor.constants import (
    ModelArmorMethod,
    ScreeningStage,
)

_FACTORY = (
    "safety_plugins.plugins.model_armor.client."
    "modelarmor_v1.ModelArmorAsyncClient"
)


def _client() -> ModelArmorClient:
    return ModelArmorClient(
        project_id="test-project",
        location_id="us-central1",
        template_id="test-template",
    )


@pytest.mark.parametrize("method", list(ModelArmorMethod))
@pytest.mark.asyncio
async def test_rpc_uses_regional_template_and_bounded_deadline(method):
    sdk = MagicMock(spec=modelarmor_v1.ModelArmorAsyncClient)
    sdk.sanitize_user_prompt = AsyncMock()
    sdk.sanitize_model_response = AsyncMock()
    with patch(_FACTORY, return_value=sdk) as factory:
        await _client().sanitize(method, "Hello", timeout_s=2.5)

    endpoint = factory.call_args.kwargs["client_options"].api_endpoint
    assert endpoint == "modelarmor.us-central1.rep.googleapis.com"
    rpc = (
        sdk.sanitize_user_prompt
        if method == ModelArmorMethod.SANITIZE_USER_PROMPT
        else sdk.sanitize_model_response
    )
    kwargs = rpc.await_args.kwargs
    assert kwargs["request"].name == (
        "projects/test-project/locations/us-central1/templates/test-template"
    )
    assert kwargs["timeout"] == 2.5
    assert kwargs["retry"] is None


@pytest.mark.asyncio
async def test_unknown_method_never_initializes_the_google_client():
    with patch(_FACTORY) as factory:
        with pytest.raises(UnsupportedModelArmorMethodError):
            await _client().sanitize("invalid", "Hello", timeout_s=1)
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_close_does_not_initialize_an_unused_client():
    with patch(_FACTORY) as factory:
        await _client().close()
        factory.assert_not_called()


@pytest.mark.asyncio
async def test_application_deadline_cancels_a_hanging_rpc(monkeypatch):
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = MagicMock(spec=ModelArmorClient)
    client.sanitize = hang
    plugin = ModelArmorSafetyFilterPlugin(client=client, timeout_s=0.01)
    monkeypatch.setattr(
        "safety_plugins.plugins.model_armor.plugin."
        "_APPLICATION_TIMEOUT_GRACE_S",
        0,
    )

    assert await plugin.scan(ScreeningStage.USER_PROMPT, "Hello") is None
    assert cancelled.is_set()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeouts_are_rejected(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        ModelArmorSafetyFilterPlugin(client=_client(), timeout_s=timeout)


def test_environment_is_resolved_at_construction(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "model-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("GOOGLE_CLOUD_MODEL_ARMOR_PROJECT", "armor-project")
    monkeypatch.setenv("GOOGLE_CLOUD_MODEL_ARMOR_LOCATION", "us-central1")
    monkeypatch.setenv("MODEL_ARMOR_TEMPLATE_ID", "armor-template")
    monkeypatch.setenv("MODEL_ARMOR_TIMEOUT_S", "3")

    plugin = ModelArmorSafetyFilterPlugin()

    assert plugin.timeout_s == 3
    assert plugin.client._template_name == (
        "projects/armor-project/locations/us-central1/templates/armor-template"
    )


def test_global_is_not_a_model_armor_region():
    with pytest.raises(ValueError, match="regional location"):
        ModelArmorClient(
            project_id="test-project",
            location_id="global",
            template_id="test-template",
        )
