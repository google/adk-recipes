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

"""Behavior tests for the Model Armor safety plugin."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import InMemoryRunner
from google.cloud import modelarmor_v1
from google.genai import types

from safety_plugins.agent import root_agent
from safety_plugins.plugins.model_armor import ModelArmorSafetyFilterPlugin
from safety_plugins.plugins.model_armor.constants import (
    SAFETY_SERVICE_UNAVAILABLE_MESSAGE,
    UNSAFE_PROMPT_STATE_KEY,
)

_DEIDENTIFIED_TEXT = "Contact me at *** for more information."


class _CaptureModelRequestPlugin(BasePlugin):
    """Capture the final model request at the ADK plugin boundary."""

    def __init__(self) -> None:
        super().__init__(name="capture_model_request")
        self.contents: list[types.Content] = []

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        self.contents = list(llm_request.contents)


def _user_prompt_response(
    filter_results: dict[str, modelarmor_v1.FilterResult],
    *,
    invocation_result: modelarmor_v1.InvocationResult = (
        modelarmor_v1.InvocationResult.SUCCESS
    ),
    match_state: modelarmor_v1.FilterMatchState = (
        modelarmor_v1.FilterMatchState.MATCH_FOUND
    ),
) -> modelarmor_v1.SanitizeUserPromptResponse:
    return modelarmor_v1.SanitizeUserPromptResponse(
        sanitization_result=modelarmor_v1.SanitizationResult(
            invocation_result=invocation_result,
            filter_match_state=match_state,
            filter_results=filter_results,
        )
    )


def _sdp_deidentify_filter(
    *,
    info_types: tuple[str, ...] = ("EMAIL_ADDRESS",),
) -> modelarmor_v1.FilterResult:
    return modelarmor_v1.FilterResult(
        sdp_filter_result=modelarmor_v1.SdpFilterResult(
            deidentify_result=modelarmor_v1.SdpDeidentifyResult(
                execution_state=(
                    modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
                ),
                match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
                data=modelarmor_v1.DataItem(text=_DEIDENTIFIED_TEXT),
                info_types=info_types,
            )
        )
    )


def _sdp_inspect_filter() -> modelarmor_v1.FilterResult:
    return modelarmor_v1.FilterResult(
        sdp_filter_result=modelarmor_v1.SdpFilterResult(
            inspect_result=modelarmor_v1.SdpInspectResult(
                execution_state=(
                    modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
                ),
                match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
                findings=[modelarmor_v1.SdpFinding(info_type="EMAIL_ADDRESS")],
            )
        )
    )


def _prompt_injection_filter() -> modelarmor_v1.FilterResult:
    return modelarmor_v1.FilterResult(
        pi_and_jailbreak_filter_result=(
            modelarmor_v1.PiAndJailbreakFilterResult(
                execution_state=(
                    modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
                ),
                match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
            )
        )
    )


def _virus_filter() -> modelarmor_v1.FilterResult:
    return modelarmor_v1.FilterResult(
        virus_scan_filter_result=modelarmor_v1.VirusScanFilterResult(
            execution_state=(
                modelarmor_v1.FilterExecutionState.EXECUTION_SUCCESS
            ),
            match_state=modelarmor_v1.FilterMatchState.MATCH_FOUND,
        )
    )


@pytest.fixture
def plugin_with_client() -> tuple[ModelArmorSafetyFilterPlugin, MagicMock]:
    client = MagicMock()
    client.sanitize_user_prompt = AsyncMock()
    client.sanitize_model_response = AsyncMock()
    client.transport.close = AsyncMock()
    with patch(
        "safety_plugins.plugins.model_armor.client.modelarmor_v1.ModelArmorAsyncClient",
        return_value=client,
    ):
        plugin = ModelArmorSafetyFilterPlugin(
            project_id="test-project",
            location_id="us-central1",
            template_id="test-template",
        )
        yield plugin, client


@pytest.mark.asyncio
async def test_sdp_replacement_screens_the_complete_user_message(
    plugin_with_client: tuple[ModelArmorSafetyFilterPlugin, MagicMock],
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    invocation_context = MagicMock()
    invocation_context.session.state = {}
    user_message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(text="Contact me at"),
            types.Part.from_text(text="alex@example.com for more information."),
        ],
    )

    replacement = await plugin.on_user_message_callback(
        invocation_context=invocation_context,
        user_message=user_message,
    )

    assert replacement is not None
    assert replacement.role == "user"
    assert replacement.parts[0].text == _DEIDENTIFIED_TEXT
    request = client.sanitize_user_prompt.call_args.kwargs["request"]
    assert request.user_prompt_data.text == (
        "Contact me at\nalex@example.com for more information."
    )
    assert UNSAFE_PROMPT_STATE_KEY not in invocation_context.session.state


@pytest.mark.asyncio
async def test_runner_sends_only_deidentified_text_to_the_model(
    plugin_with_client: tuple[ModelArmorSafetyFilterPlugin, MagicMock],
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    client.sanitize_model_response.return_value = (
        modelarmor_v1.SanitizeModelResponseResponse(
            sanitization_result=modelarmor_v1.SanitizationResult(
                invocation_result=modelarmor_v1.InvocationResult.SUCCESS,
                filter_match_state=(
                    modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
                ),
            )
        )
    )
    capture_plugin = _CaptureModelRequestPlugin()
    app = App(
        name="model_armor_sdp_test",
        root_agent=root_agent,
        plugins=[plugin, capture_plugin],
    )
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="test-user",
    )

    async for _ in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=types.UserContent("Contact me at alex@example.com."),
    ):
        pass

    model_input = "\n".join(
        part.text or ""
        for content in capture_plugin.contents
        for part in (content.parts or [])
    )
    assert _DEIDENTIFIED_TEXT in model_input
    assert "alex@example.com" not in model_input


@pytest.mark.parametrize(
    "filter_results",
    [
        {"sdp": _sdp_inspect_filter()},
        {
            "sdp": _sdp_deidentify_filter(),
            "pi_and_jailbreak": _prompt_injection_filter(),
        },
        {
            "sdp": _sdp_deidentify_filter(info_types=()),
            "virus_scan": _virus_filter(),
        },
    ],
    ids=["inspect-only", "prompt-injection", "unparsed-filter"],
)
@pytest.mark.asyncio
async def test_sdp_match_without_an_exclusive_replacement_is_blocked(
    plugin_with_client: tuple[ModelArmorSafetyFilterPlugin, MagicMock],
    filter_results: dict[str, modelarmor_v1.FilterResult],
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        filter_results
    )
    invocation_context = MagicMock()
    invocation_context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=invocation_context,
        user_message=types.UserContent("Email alex@example.com"),
    )

    assert replacement is not None
    assert "removed" in replacement.parts[0].text
    assert invocation_context.session.state[UNSAFE_PROMPT_STATE_KEY]


@pytest.mark.asyncio
async def test_model_armor_failure_blocks_the_user_prompt(
    plugin_with_client: tuple[ModelArmorSafetyFilterPlugin, MagicMock],
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {},
        invocation_result=modelarmor_v1.InvocationResult.FAILURE,
        match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
    )
    invocation_context = MagicMock()
    invocation_context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=invocation_context,
        user_message=types.UserContent("Email alex@example.com"),
    )

    assert replacement is not None
    assert replacement.parts[0].text == SAFETY_SERVICE_UNAVAILABLE_MESSAGE
    assert invocation_context.session.state[UNSAFE_PROMPT_STATE_KEY]


@pytest.mark.asyncio
async def test_unspecified_verdict_blocks_the_user_prompt(
    plugin_with_client,
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {},
        match_state=modelarmor_v1.FilterMatchState.FILTER_MATCH_STATE_UNSPECIFIED,
    )
    context = MagicMock()
    context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=context,
        user_message=types.UserContent("Hello"),
    )

    assert replacement is not None
    assert "Hello" not in replacement.parts[0].text
    assert await plugin.before_run_callback(invocation_context=context)


@pytest.mark.asyncio
async def test_rpc_timeout_blocks_without_leaking_error_text(
    plugin_with_client,
    caplog,
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.side_effect = TimeoutError(
        "request contained alex@example.com"
    )
    context = MagicMock()
    context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=context,
        user_message=types.UserContent("Email alex@example.com"),
    )

    assert replacement is not None
    assert "alex@example.com" not in replacement.parts[0].text
    assert "alex@example.com" not in caplog.text
    assert await plugin.before_run_callback(invocation_context=context)


@pytest.mark.asyncio
async def test_sdp_model_response_uses_transformed_text(
    plugin_with_client,
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_model_response.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    response = LlmResponse(content=types.ModelContent("alex@example.com"))

    replacement = await plugin.after_model_callback(
        callback_context=MagicMock(), llm_response=response
    )

    assert replacement is not None
    assert replacement.content.parts[0].text == _DEIDENTIFIED_TEXT


@pytest.mark.asyncio
async def test_redaction_preserves_tool_call_and_signature(plugin_with_client):
    plugin, client = plugin_with_client
    client.sanitize_model_response.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    call = types.Part(
        function_call=types.FunctionCall(
            name="lookup_contact", args={"key": "contact"}, id="call-1"
        ),
        thought_signature=b"opaque-signature",
    )
    expected_call = call.model_copy(deep=True)
    response = LlmResponse(
        content=types.Content(
            role="model", parts=[call, types.Part(text="alex@example.com")]
        )
    )
    replacement = await plugin.after_model_callback(
        callback_context=MagicMock(), llm_response=response
    )
    assert replacement.content.parts == [
        expected_call,
        types.Part(text=_DEIDENTIFIED_TEXT),
    ]
    request = client.sanitize_model_response.call_args.kwargs["request"]
    assert request.model_response_data.text == "alex@example.com"


@pytest.mark.parametrize(
    "parts",
    [
        [
            types.Part(text="alex@example.com", thought=True),
            types.Part(text="Public answer"),
        ],
        [types.Part(text="alex@example.com"), types.Part(text="More text")],
        [types.Part(text="alex@example.com", thought_signature=b"signed")],
        [
            types.Part(
                text="alex@example.com",
                function_call=types.FunctionCall(
                    name="lookup_contact", args={}
                ),
            )
        ],
    ],
    ids=["thought-public", "multiple-public", "signed", "two-payloads"],
)
@pytest.mark.asyncio
async def test_ambiguous_model_redaction_blocks(
    plugin_with_client, parts, caplog
):
    plugin, client = plugin_with_client
    client.sanitize_model_response.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    with caplog.at_level("INFO"):
        replacement = await plugin.after_model_callback(
            callback_context=MagicMock(),
            llm_response=LlmResponse(
                content=types.Content(role="model", parts=parts)
            ),
        )
    assert "removed" in replacement.content.parts[0].text
    assert "alex@example.com" not in replacement.model_dump_json()
    assert _DEIDENTIFIED_TEXT not in replacement.model_dump_json()
    verdicts = [
        r.model_armor for r in caplog.records if hasattr(r, "model_armor")
    ]
    assert verdicts[-1]["action"] == "block"


@pytest.mark.parametrize("extra_text", [False, True])
@pytest.mark.asyncio
async def test_user_redaction_with_media(plugin_with_client, extra_text):
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    media = types.Part.from_bytes(data=b"synthetic", mime_type="image/png")
    expected_media = media.model_copy(deep=True)
    parts = [types.Part(text=" alex@example.com "), media]
    if extra_text:
        parts.append(types.Part(text="More text"))
    context = MagicMock()
    context.session.state = {}
    replacement = await plugin.on_user_message_callback(
        invocation_context=context,
        user_message=types.Content(role="user", parts=parts),
    )
    if extra_text:
        assert "removed" in replacement.parts[0].text
        assert context.session.state[UNSAFE_PROMPT_STATE_KEY]
    else:
        assert replacement.parts == [
            types.Part(text=_DEIDENTIFIED_TEXT),
            expected_media,
        ]
        request = client.sanitize_user_prompt.call_args.kwargs["request"]
        assert request.user_prompt_data.text == " alex@example.com "


@pytest.mark.asyncio
async def test_allow_preserves_mixed_content_exactly(plugin_with_client):
    plugin, client = plugin_with_client
    client.sanitize_model_response.return_value = _user_prompt_response(
        {}, match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    text="Thought", thought=True, thought_signature=b"signed"
                ),
                types.Part(text="Public text"),
                types.Part.from_function_call(name="lookup_contact", args={}),
            ],
        )
    )
    original = response.model_dump_json()
    assert (
        await plugin.after_model_callback(
            callback_context=MagicMock(), llm_response=response
        )
        is None
    )
    assert response.model_dump_json() == original


@pytest.mark.parametrize(
    "invocation_result",
    [
        modelarmor_v1.InvocationResult.PARTIAL,
        modelarmor_v1.InvocationResult.FAILURE,
        modelarmor_v1.InvocationResult.INVOCATION_RESULT_UNSPECIFIED,
    ],
)
@pytest.mark.asyncio
async def test_incomplete_scan_never_forwards_an_sdp_replacement(
    plugin_with_client,
    invocation_result,
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()},
        invocation_result=invocation_result,
    )
    context = MagicMock()
    context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=context,
        user_message=types.UserContent("Email alex@example.com"),
    )

    assert replacement.parts[0].text == SAFETY_SERVICE_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize(
    "filters, state",
    [
        ({}, modelarmor_v1.FilterMatchState.MATCH_FOUND),
        (
            {"pi_and_jailbreak": _prompt_injection_filter()},
            modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
        ),
    ],
)
@pytest.mark.asyncio
async def test_inconsistent_scan_is_unavailable(
    plugin_with_client,
    filters,
    state,
) -> None:
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        filters,
        match_state=state,
    )
    context = MagicMock()
    context.session.state = {}

    replacement = await plugin.on_user_message_callback(
        invocation_context=context, user_message=types.UserContent("Hello")
    )

    assert replacement.parts[0].text == SAFETY_SERVICE_UNAVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_tool_output_preserves_redacted_json_structure(
    plugin_with_client,
) -> None:
    plugin, client = plugin_with_client
    response = _user_prompt_response({"sdp": _sdp_deidentify_filter()})
    response.sanitization_result.filter_results[
        "sdp"
    ].sdp_filter_result.deidentify_result.data.text = (
        '{"email": "***", "count": 2}'
    )
    client.sanitize_user_prompt.return_value = response

    replacement = await plugin.after_tool_callback(
        tool=MagicMock(name="lookup"),
        tool_args={},
        tool_context=MagicMock(),
        result={"email": "alex@example.com", "count": 2},
    )

    assert replacement == {"email": "***", "count": 2}
    client.sanitize_user_prompt.assert_awaited_once()
    client.sanitize_model_response.assert_not_awaited()


@pytest.mark.parametrize("stage", ["model", "tool", "callback"])
@pytest.mark.asyncio
async def test_non_sdp_matches_block_every_stage(plugin_with_client, stage):
    plugin, client = plugin_with_client
    response = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter(), "virus_scan": _virus_filter()}
    )
    client.sanitize_user_prompt.return_value = response
    client.sanitize_model_response.return_value = response

    if stage == "model":
        result = await plugin.after_model_callback(
            callback_context=MagicMock(),
            llm_response=LlmResponse(content=types.ModelContent("unsafe")),
        )
        assert "removed" in result.content.parts[0].text
    elif stage == "tool":
        result = await plugin.after_tool_callback(
            tool=MagicMock(),
            tool_args={},
            tool_context=MagicMock(),
            result={"message": "unsafe"},
        )
        assert "error" in result
    else:
        assert await plugin.screen_external_payload({"text": "unsafe"}) is None


@pytest.mark.asyncio
async def test_runner_blocks_before_model_and_recovers_next_turn(
    plugin_with_client,
) -> None:
    plugin, client = plugin_with_client
    safe = _user_prompt_response(
        {}, match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    client.sanitize_user_prompt.side_effect = [
        _user_prompt_response({"pi_and_jailbreak": _prompt_injection_filter()}),
        safe,
    ]
    client.sanitize_model_response.return_value = safe
    capture = _CaptureModelRequestPlugin()
    runner = InMemoryRunner(
        app=App(
            name="blocked_turn_test",
            root_agent=root_agent,
            plugins=[plugin, capture],
        )
    )
    async with runner:
        session = await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id="test-user",
        )
        events = [
            event
            async for event in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.UserContent("UNSAFE_SENTINEL"),
            )
        ]
        assert not capture.contents
        assert "removed" in events[-1].content.parts[0].text
        async for _ in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=types.UserContent("Hello again"),
        ):
            pass
        assert capture.contents
        stored = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )
        assert "UNSAFE_SENTINEL" not in stored.model_dump_json()
        assert not stored.state.get(UNSAFE_PROMPT_STATE_KEY)
    client.transport.close.assert_awaited_once()


@pytest.mark.parametrize(
    "filter_result",
    [
        {},
        {
            "pi_and_jailbreak_filter_result": {
                "execution_state": "EXECUTION_SKIPPED",
                "match_state": "NO_MATCH_FOUND",
            }
        },
        {
            "pi_and_jailbreak_filter_result": {
                "execution_state": "EXECUTION_SUCCESS",
            }
        },
    ],
    ids=["empty-filter", "skipped-filter", "unspecified-verdict"],
)
@pytest.mark.asyncio
async def test_runner_blocks_incomplete_filter_despite_global_success(
    plugin_with_client, filter_result
):
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"pi_and_jailbreak": modelarmor_v1.FilterResult(filter_result)},
        match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND,
    )
    client.sanitize_model_response.return_value = _user_prompt_response(
        {}, match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    capture = _CaptureModelRequestPlugin()
    async with InMemoryRunner(
        app=App(
            name="incomplete_filter_test",
            root_agent=root_agent,
            plugins=[plugin, capture],
        )
    ) as runner:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="test-user"
        )
        events = [
            event
            async for event in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.UserContent("UNSCREENED_SENTINEL"),
            )
        ]
        stored = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )

    assert not capture.contents
    assert (
        events[-1].content.parts[0].text == SAFETY_SERVICE_UNAVAILABLE_MESSAGE
    )
    assert "UNSCREENED_SENTINEL" not in stored.model_dump_json()


@pytest.mark.asyncio
async def test_redaction_logs_only_verdict_metadata(plugin_with_client, caplog):
    plugin, client = plugin_with_client
    client.sanitize_user_prompt.return_value = _user_prompt_response(
        {"sdp": _sdp_deidentify_filter()}
    )
    context = MagicMock()
    context.session.state = {}
    with caplog.at_level("INFO", logger="safety_plugins.plugins.model_armor"):
        await plugin.on_user_message_callback(
            invocation_context=context,
            user_message=types.UserContent("Email alex@example.com"),
        )

    metadata = [
        record.model_armor
        for record in caplog.records
        if hasattr(record, "model_armor")
    ]
    assert metadata[0]["action"] == "redact"
    assert metadata[0]["sdp"]["info_types"] == ["EMAIL_ADDRESS"]
    assert "alex@example.com" not in repr(metadata)
    assert _DEIDENTIFIED_TEXT not in repr(metadata)


@pytest.mark.asyncio
async def test_runner_saves_and_forwards_only_the_redacted_tool_result(
    plugin_with_client,
):
    from scripts.live_model_armor import ProbeModel

    plugin, client = plugin_with_client
    safe = _user_prompt_response(
        {}, match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    redacted = _user_prompt_response({"sdp": _sdp_deidentify_filter()})
    redacted.sanitization_result.filter_results[
        "sdp"
    ].sdp_filter_result.deidentify_result.data.text = '{"email": "***"}'
    client.sanitize_user_prompt.side_effect = [safe, redacted]
    client.sanitize_model_response.return_value = safe

    def lookup_contact() -> dict[str, str]:
        """Return the synthetic address."""
        return {"email": "alex@example.com"}

    model = ProbeModel(output="Contact retrieved.", use_tool=True)
    async with InMemoryRunner(
        app=App(
            name="tool_redaction_test",
            root_agent=LlmAgent(
                name="probe", model=model, tools=[lookup_contact]
            ),
            plugins=[plugin],
        )
    ) as runner:
        session = await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id="test-user",
        )
        async for _ in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=types.UserContent("Look up the synthetic contact"),
        ):
            pass
        stored = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )

    assert len(model.seen) == 2
    assert '"email": "***"' in model.seen[-1]
    assert "alex@example.com" not in "\n".join(model.seen)
    assert "alex@example.com" not in stored.model_dump_json()


@pytest.mark.asyncio
async def test_plugin_screens_content_across_agent_transfer(plugin_with_client):
    from scripts.live_model_armor import ProbeModel

    class TransferModel(ProbeModel):
        async def generate_content_async(self, llm_request, stream=False):
            self.seen.append(
                "\n".join(c.model_dump_json() for c in llm_request.contents)
            )
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="transfer_to_agent",
                            args={"agent_name": "worker"},
                        )
                    ],
                )
            )

    plugin, client = plugin_with_client
    redacted = _user_prompt_response({"sdp": _sdp_deidentify_filter()})
    safe = _user_prompt_response(
        {}, match_state=modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    # The user event is transformed once; the transfer tool's result is safe.
    client.sanitize_user_prompt.side_effect = [redacted, safe]
    client.sanitize_model_response.return_value = redacted
    parent_model = TransferModel()
    child_model = ProbeModel(output="alex@example.com")
    async with InMemoryRunner(
        app=App(
            name="transfer_screening_test",
            root_agent=LlmAgent(
                name="coordinator",
                model=parent_model,
                sub_agents=[LlmAgent(name="worker", model=child_model)],
            ),
            plugins=[plugin],
        )
    ) as runner:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="test-user"
        )
        events = [
            event
            async for event in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.UserContent("Contact alex@example.com"),
            )
        ]
        stored = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=session.user_id,
            session_id=session.id,
        )

    assert len(parent_model.seen) == len(child_model.seen) == 1
    assert _DEIDENTIFIED_TEXT in parent_model.seen[0]
    assert _DEIDENTIFIED_TEXT in child_model.seen[0]
    assert "alex@example.com" not in parent_model.seen[0]
    assert "alex@example.com" not in child_model.seen_contents[0]
    finals = [event for event in events if event.is_final_response()]
    assert len(finals) == 1
    assert finals[0].author == "worker"
    assert finals[0].content.parts[0].text == _DEIDENTIFIED_TEXT
    assert "alex@example.com" not in stored.model_dump_json()
    assert all("alex@example.com" not in e.model_dump_json() for e in events)
