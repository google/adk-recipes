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

from unittest.mock import MagicMock, patch

import pytest
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from presentation_agent.shared_libraries.model_armor import (
    model_armor_interceptor,
    model_armor_response_interceptor,
)


@pytest.fixture
def mock_callback_context():
    ctx = MagicMock(spec=CallbackContext)
    ctx.user_content = types.Content(
        role="user", parts=[types.Part.from_text(text="Hello there")]
    )
    return ctx


@pytest.fixture
def mock_llm_response():
    resp = MagicMock(spec=LlmResponse)
    resp.content = types.Content(
        role="model",
        parts=[types.Part.from_text(text="This is a safe response")],
    )
    return resp


def create_mock_response(json_data):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("httpx.AsyncClient.post")
@pytest.mark.asyncio
async def test_model_armor_interceptor_allow(
    mock_post, mock_auth, mock_callback_context
):
    mock_auth.return_value = (MagicMock(), "test-project")
    mock_post.return_value = create_mock_response(
        {
            "sanitizationResult": {
                "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_ALLOW"
            }
        }
    )

    result = await model_armor_interceptor(mock_callback_context)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("httpx.AsyncClient.post")
@pytest.mark.asyncio
async def test_model_armor_interceptor_block(
    mock_post, mock_auth, mock_callback_context
):
    mock_auth.return_value = (MagicMock(), "test-project")
    mock_post.return_value = create_mock_response(
        {
            "sanitizationResult": {
                "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK",
                "sanitizationVerdictReason": "Prompt Injection Detected",
            }
        }
    )

    result = await model_armor_interceptor(mock_callback_context)
    assert result is not None
    assert "enterprise AI security policy" in result.parts[0].text


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("httpx.AsyncClient.post")
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_allow(
    mock_post, mock_auth, mock_callback_context, mock_llm_response
):
    mock_auth.return_value = (MagicMock(), "test-project")
    mock_post.return_value = create_mock_response(
        {
            "sanitizationResult": {
                "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_ALLOW"
            }
        }
    )

    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("httpx.AsyncClient.post")
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_redact(
    mock_post, mock_auth, mock_callback_context, mock_llm_response
):
    mock_auth.return_value = (MagicMock(), "test-project")
    mock_post.return_value = create_mock_response(
        {
            "sanitizationResult": {
                "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_ALLOW",
                "filterMatchResults": [
                    {
                        "sdpFilterMatchResult": {
                            "redactedContent": "My card is [REDACTED]"
                        }
                    }
                ],
            }
        }
    )

    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is not None
    assert "[REDACTED]" in result.content.parts[0].text


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("presentation_agent.shared_libraries.model_armor.os.getenv")
@pytest.mark.asyncio
async def test_model_armor_interceptor_no_project(
    mock_getenv, mock_auth, mock_callback_context
):
    mock_auth.return_value = (MagicMock(), None)
    # Make getenv return None for GOOGLE_CLOUD_PROJECT
    mock_getenv.side_effect = lambda k, d=None: (
        d if k != "GOOGLE_CLOUD_PROJECT" else None
    )

    result = await model_armor_interceptor(mock_callback_context)
    # Fail-closed logic returns a types.Content since data is None
    assert result is not None
    assert "Security Verification Error" in result.parts[0].text


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@pytest.mark.asyncio
async def test_model_armor_interceptor_exception(
    mock_auth, mock_callback_context
):
    mock_auth.side_effect = Exception("Auth failed")

    result = await model_armor_interceptor(mock_callback_context)
    # Since _call_model_armor_api returns None on exception, we hit fail-closed
    assert result is not None
    assert "Security Verification Error" in result.parts[0].text


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor._call_model_armor_api")
@pytest.mark.asyncio
async def test_model_armor_interceptor_mocked_allow(
    mock_call_api,
    mock_callback_context,
):
    mock_call_api.return_value = {
        "sanitizationResult": {
            "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_ALLOW"
        }
    }
    result = await model_armor_interceptor(mock_callback_context)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    None,
)
@pytest.mark.asyncio
async def test_model_armor_interceptor_no_template_id(mock_callback_context):
    result = await model_armor_interceptor(mock_callback_context)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@pytest.mark.asyncio
async def test_model_armor_interceptor_no_user_content():
    ctx = MagicMock(spec=CallbackContext)
    ctx.user_content = None
    result = await model_armor_interceptor(ctx)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@pytest.mark.asyncio
async def test_model_armor_interceptor_no_user_text():
    ctx = MagicMock(spec=CallbackContext)
    ctx.user_content = types.Content(role="user", parts=[])
    result = await model_armor_interceptor(ctx)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    None,
)
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_no_template_id(
    mock_callback_context, mock_llm_response
):
    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_no_llm_content(
    mock_callback_context,
):
    resp = MagicMock(spec=LlmResponse)
    resp.content = None
    result = await model_armor_response_interceptor(mock_callback_context, resp)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_no_llm_text(
    mock_callback_context,
):
    resp = MagicMock(spec=LlmResponse)
    resp.content = types.Content(role="model", parts=[])
    result = await model_armor_response_interceptor(mock_callback_context, resp)
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_fail_closed(
    mock_auth, mock_callback_context, mock_llm_response
):
    mock_auth.side_effect = Exception("Auth failed")

    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is not None
    assert "Security Verification Error" in result.content.parts[0].text


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor._call_model_armor_api")
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_mocked_allow(
    mock_call_api,
    mock_callback_context,
    mock_llm_response,
):
    mock_call_api.return_value = {
        "sanitizationResult": {
            "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_ALLOW"
        }
    }
    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is None


@patch(
    "presentation_agent.shared_libraries.model_armor.MODEL_ARMOR_TEMPLATE_ID",
    "test-template",
)
@patch("presentation_agent.shared_libraries.model_armor.google.auth.default")
@patch("httpx.AsyncClient.post")
@pytest.mark.asyncio
async def test_model_armor_response_interceptor_block(
    mock_post, mock_auth, mock_callback_context, mock_llm_response
):
    mock_auth.return_value = (MagicMock(), "test-project")
    mock_post.return_value = create_mock_response(
        {
            "sanitizationResult": {
                "sanitizationVerdict": "MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK",
                "sanitizationVerdictReason": "Toxicity Detected",
            }
        }
    )

    result = await model_armor_response_interceptor(
        mock_callback_context, mock_llm_response
    )
    assert result is not None
    assert (
        "blocked by enterprise security policy" in result.content.parts[0].text
    )
