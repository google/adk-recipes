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

"""Unit tests for the session reviewer's agent loop.

Every Gemini model call goes to a fake `google.genai.Client` behind ADK's real
`Gemini` model, and every Claude model call goes to `FakeAnthropicVertex`
behind ADK's real `Claude` model, so the ADK request path runs and nothing
reaches a model. The jq tool runs for real. All session data is synthetic.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import threading
from typing import Any, ClassVar

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    ToolDefinition,
)
from ambient_quality_agent.tools.ingestion.message_parts import (
    RUNTIME_ERROR_RESPONSES,
)
from ambient_quality_agent.tools.review import agentic_reviewer
from ambient_quality_agent.tools.review.agentic_reviewer import (
    agent,
    final_answer,
    prompts,
)
from ambient_quality_agent.tools.review.agentic_reviewer.reviewer import (
    ReviewDeadlineError,
    ReviewOutcome,
)
from ambient_quality_agent.tools.review.agentic_reviewer.tools.jq import (
    QUERY_TOOL,
    JqQueryTool,
    ToolOutput,
    build_query_declaration,
)
from anthropic import types as anthropic_types
from google import genai
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

WORKER = "worker"

FINDING = {
    "agent_id": WORKER,
    "expected_behavior": "not refund a shipped order",
    "actual_behavior": "called refund at t0.e2",
}


class FakeModels:
    """Answers model calls from a script, synchronously and asynchronously.

    Each entry is a response or a callable that takes the request config and
    returns a response. ADK calls this through ``aio.models``; each call
    appends the request to `requests` and the model name to `model_names`.
    """

    def __init__(self, script: list[Any]) -> None:
        """Initialize the fake models with a response script.

        Args:
            script: Responses or callables returning responses, consumed in
                order.
        """
        self.script = list(script)
        self.requests: list[
            tuple[list[types.Content], types.GenerateContentConfig]
        ] = []
        self.model_names: list[str] = []
        self.lock = threading.Lock()

    def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        """Record the request and return the next scripted response.

        Args:
            model: Model identifier.
            contents: Request conversation contents.
            config: Request generation configuration.

        Returns:
            The scripted response.
        """
        with self.lock:
            self.model_names.append(model)
            self.requests.append(
                (
                    [content.model_copy(deep=True) for content in contents],
                    config.model_copy(deep=True),
                )
            )
            entry = self.script.pop(0)
        return entry(config) if callable(entry) else entry


class FakeAsyncModels:
    """The ``aio.models`` face of `FakeModels`."""

    def __init__(self, models: FakeModels) -> None:
        """Initialize the async wrapper around `models`.

        Args:
            models: Underlying synchronous `FakeModels` instance.
        """
        self._models = models

    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        """Answer like `FakeModels.generate_content`.

        Args:
            model: Model identifier.
            contents: Request conversation contents.
            config: Request generation configuration.

        Returns:
            The scripted response.
        """
        return self._models.generate_content(
            model=model, contents=contents, config=config
        )


class _FakeAio:
    """Holds the asynchronous models of a fake client."""

    def __init__(self, models: FakeModels) -> None:
        """Initialize the async namespace.

        Args:
            models: Underlying `FakeModels` instance.
        """
        self.models = FakeAsyncModels(models)


class FakeClient(genai.Client):
    """A `google.genai.Client` that answers from a script.

    Subclasses `genai.Client` because ADK's `Gemini` model validates the
    client's type, while skipping the base constructor to avoid network or
    credential setup.
    """

    def __init__(
        self, script: list[Any], *, models: FakeModels | None = None
    ) -> None:
        """Initialize the fake client.

        Args:
            script: Responses or callables returning responses, consumed in
                order.
            models: Models that answer in place of `script`; None answers
                from `script`.
        """
        self._fake_models = models if models is not None else FakeModels(script)
        self._fake_aio = _FakeAio(self._fake_models)

    @property
    def models(self) -> FakeModels:  # type: ignore[override]
        """The synchronous models."""
        return self._fake_models

    @property
    def aio(self) -> _FakeAio:  # type: ignore[override]
        """The asynchronous models."""
        return self._fake_aio

    @property
    def vertexai(self) -> bool:
        """Report Vertex AI, so ADK keeps the request labels."""
        return True

    def close(self) -> None:
        """No-op; holds no resources."""


class SessionCapturePlugin(BasePlugin):
    """Keeps the ADK session of the run it observes and, per model call, the
    number of tool results `ReviewLoopController` first stubbed for it."""

    def __init__(self) -> None:
        """Initialize the plugin with no session and no model calls."""
        super().__init__(name="session_capture")
        self.session: Any = None
        self.pruned: list[int] = []

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> None:
        """Keep the session.

        Args:
            callback_context: The model call's context.
            llm_request: The request; unused.
        """
        self.session = callback_context.session

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> None:
        """Record the call's newly stubbed count, set before the call.

        Args:
            callback_context: The model call's context.
            llm_response: The response; unused.
        """
        self.pruned.append(callback_context.state[agent.PRUNED_STATE_KEY])


def _build_case(case_id: str = "session-1") -> EvalCase:
    """Build a one-turn session with a tool call and a long result.

    Args:
        case_id: Evaluation case id.

    Returns:
        The evaluation case.
    """
    events = [
        {
            "author": "user",
            "content": {"role": "user", "parts": [{"text": "Refund order 7"}]},
        },
        {
            "author": WORKER,
            "content": {
                "role": "model",
                "parts": [
                    {
                        "function_call": {
                            "name": "lookup",
                            "args": {"order": "7"},
                        }
                    }
                ],
            },
        },
        {
            "author": WORKER,
            "content": {
                "role": "model",
                "parts": [
                    {
                        "function_response": {
                            "name": "lookup",
                            "response": {
                                "status": "SHIPPED",
                                "remarks": "Left the depot. " * 40,
                            },
                        }
                    }
                ],
            },
        },
        {
            "author": WORKER,
            "content": {"role": "model", "parts": [{"text": "Refunded."}]},
        },
    ]
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {WORKER: {"agent_id": WORKER}},
                "turns": [
                    {"turn_index": 0, "turn_id": "turn-0", "events": events}
                ],
            },
        }
    )


def _build_revisions() -> list[AgentRevision]:
    """Build the worker agent configuration without tool parameter schemas.

    Returns:
        Single-element list containing the worker `AgentRevision`.
    """
    return [
        AgentRevision(
            agent_id=WORKER,
            instruction="Never refund an order that has shipped.",
            tools=[
                ToolDefinition(name="lookup"),
                ToolDefinition(name="refund"),
            ],
        )
    ]


def _respond(
    *parts: types.Part, prompt_tokens: int = 100
) -> types.GenerateContentResponse:
    """Build a model `GenerateContentResponse` containing `parts`.

    Args:
        *parts: Content parts for the model candidate.
        prompt_tokens: Reported prompt token count.

    Returns:
        Populated `GenerateContentResponse`.
    """
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=10,
            thoughts_token_count=5,
            cached_content_token_count=2,
        ),
    )


def _query(expr: str) -> types.Part:
    """Build a `query_trajectory` function-call part without a call ID.

    Args:
        expr: jq expression argument.

    Returns:
        Function-call `Part`.
    """
    return types.Part(
        function_call=types.FunctionCall(name=QUERY_TOOL, args={"expr": expr})
    )


def _answer(
    findings: list[dict[str, str]] | None = None,
) -> types.GenerateContentResponse:
    """Build a final JSON text response containing `findings`.

    Args:
        findings: Reported findings; None defaults to an empty list.

    Returns:
        `GenerateContentResponse` with the serialized findings JSON.
    """
    return _respond(
        types.Part(
            text=json.dumps({"findings": findings or [], "dismissed": []})
        )
    )


def _review(
    client: FakeClient, plugins: tuple[BasePlugin, ...] = ()
) -> ReviewOutcome:
    """Run the ADK reviewer on the sample case using `client`.

    Args:
        client: Fake Gemini client.
        plugins: ADK plugins observing the run.

    Returns:
        `ReviewOutcome` from `agentic_reviewer.review_session`.
    """
    return agentic_reviewer.review_session(
        _build_case(), _build_revisions(), client=client, plugins=plugins
    )


def _list_function_responses(
    contents: list[types.Content],
) -> list[types.FunctionResponse]:
    """List all `FunctionResponse` payloads across `contents` in order.

    Args:
        contents: Request conversation contents.

    Returns:
        Ordered list of `FunctionResponse` objects.
    """
    return [
        part.function_response
        for content in contents
        for part in content.parts or []
        if part.function_response is not None
    ]


def _set_prune_watermark(monkeypatch: pytest.MonkeyPatch, tokens: int) -> None:
    """Set `config.review_prune_watermark_tokens` for one test.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tokens: Prune watermark in prompt tokens.
    """
    from .conftest import make_config

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(review_prune_watermark_tokens=tokens),
    )


def test_tool_calls_then_a_text_answer_return_the_parsed_findings() -> None:
    client = FakeClient(
        [
            _respond(_query(".agents | map(.agent_id)")),
            _answer([FINDING, {**FINDING, "agent_id": "ghost"}]),
        ]
    )

    outcome = _review(client)

    assert [(f.agent_id, f.actual_behavior) for f in outcome.findings] == [
        (WORKER, "called refund at t0.e2"),
        ("", "called refund at t0.e2"),
    ]
    assert all(f.trajectory_id == "session-1" for f in outcome.findings)
    assert outcome.parse_path == final_answer.FINAL_PARSE_STRICT
    assert outcome.reasked is False
    assert outcome.forced is False
    contents, _ = client.models.requests[1]
    (response,) = _list_function_responses(contents)
    assert response.response == {"output": '["worker"]'}
    assert response.id is None


def test_the_request_carries_the_exact_instruction_and_declaration() -> None:
    client = FakeClient([_answer()])

    _review(client)

    ((contents, config),) = client.models.requests
    prompt = agentic_reviewer.build_review_prompt(
        _build_case(), _build_revisions()
    )
    assert config.system_instruction == prompt.system_instruction
    assert config.system_instruction == (
        prompts.resolve_system_instruction() + prompts.UNRECORDED_SCHEMAS_RULE
    )
    assert "You are an agent" not in str(config.system_instruction)
    assert [content.role for content in contents] == ["user"]
    assert contents[0].parts[0].text == prompt.initial_message  # type: ignore[index]
    assert config.tools == [
        types.Tool(function_declarations=[build_query_declaration()])
    ]
    assert config.tool_config is None
    assert config.thinking_config == types.ThinkingConfig(
        thinking_level=types.ThinkingLevel.LOW, include_thoughts=True
    )
    assert config.automatic_function_calling.disable is True  # type: ignore[union-attr]
    assert config.labels["adk_agent_name"] == agent.AGENT_NAME  # type: ignore[index]


def test_calls_of_one_reply_run_in_parallel_and_answer_in_call_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Barrier ensures both calls start before either finishes, so sequential
    # execution fails with a timeout rather than hanging.
    barrier = threading.Barrier(2, timeout=10)
    second_finished = threading.Event()
    finished: list[str] = []
    lock = threading.Lock()
    original = JqQueryTool.query_trajectory

    def query_after_both_start(tool: JqQueryTool, expr: str) -> ToolOutput:
        barrier.wait()
        if expr == '"first"':
            assert second_finished.wait(timeout=10)
        output = original(tool, expr)
        with lock:
            finished.append(expr)
        if expr == ".[":
            second_finished.set()
        return output

    monkeypatch.setattr(JqQueryTool, "query_trajectory", query_after_both_start)
    client = FakeClient(
        [_respond(_query('"first"'), _query(".[")), _answer([FINDING])]
    )

    outcome = _review(client)

    responses = _list_function_responses(client.models.requests[1][0])
    assert [response.name for response in responses] == [QUERY_TOOL] * 2
    assert responses[0].response == {"output": "first"}
    assert list(responses[1].response or {}) == ["error"]
    assert finished == [".[", '"first"']
    assert len(outcome.findings) == 1


def test_an_undeclared_tool_gets_an_error_result() -> None:
    client = FakeClient(
        [
            _respond(
                types.Part(
                    function_call=types.FunctionCall(
                        name="grep", args={"pattern": "x"}
                    )
                )
            ),
            _answer(),
        ]
    )

    _review(client)

    (response,) = _list_function_responses(client.models.requests[1][0])
    assert (response.name, response.response) == (
        "grep",
        {"error": "unknown tool 'grep'; call query_trajectory"},
    )


def test_the_last_allowed_call_forces_the_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "MAX_MODEL_CALLS", 3)

    def respond(
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        if config.tool_config is not None:
            return _answer([FINDING])
        return _respond(_query('"x"'))

    client = FakeClient([respond, respond, respond])

    outcome = _review(client)

    configs = [config for _, config in client.models.requests]
    assert [config.tool_config for config in configs] == [
        None,
        None,
        types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.NONE
            )
        ),
    ]
    last_contents = client.models.requests[-1][0]
    assert last_contents[-1] == types.Content(
        role="user", parts=[types.Part(text=prompts.TEXT_FORCE)]
    )
    assert outcome.forced is True
    assert len(outcome.findings) == 1


def _list_sent_results(client: FakeClient) -> list[list[Any]]:
    """List the tool result payloads each model request carried.

    Args:
        client: Fake Gemini client after a review.

    Returns:
        Per request, the `FunctionResponse.response` dicts in order.
    """
    return [
        [response.response for response in _list_function_responses(contents)]
        for contents, _ in client.models.requests
    ]


def test_every_older_result_is_stubbed_when_the_target_is_out_of_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each result is about 100 tokens and its stub about 25, so stubbing the
    # few prunable results never brings the estimate down to the target.
    _set_prune_watermark(monkeypatch, 1_000)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 2)
    texts = [str(index) * 300 for index in range(4)]
    client = FakeClient(
        [
            *[
                _respond(_query(f'"{text}"'), prompt_tokens=1_001)
                for text in texts
            ],
            _answer(),
        ]
    )
    capture = SessionCapturePlugin()

    _review(client, plugins=(capture,))

    stub = {"pruned": True, "chars": 300, "note": prompts.PRUNED_NOTE}
    full = [{"output": text} for text in texts]
    assert capture.pruned == [0, 0, 0, 1, 1]
    assert _list_sent_results(client) == [
        [],
        full[:1],
        full[:2],
        [stub, *full[1:3]],
        [stub, stub, *full[2:]],
    ]
    kept = [
        response.response
        for event in capture.session.events
        for response in event.get_function_responses()
    ]
    assert kept == full


def test_no_pruning_at_the_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_prune_watermark(monkeypatch, 1_000)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    # Each result is 314 JSON characters, 104 estimated tokens, so every
    # estimate is exactly 896 + 104 = 1,000.
    text = "r" * 300
    client = FakeClient(
        [
            *[
                _respond(_query(f'"{text}"'), prompt_tokens=896)
                for _ in range(3)
            ],
            _answer(),
        ]
    )

    _review(client)

    last = _list_function_responses(client.models.requests[-1][0])
    assert [response.response for response in last] == [{"output": text}] * 3


def test_pruning_stubs_the_oldest_results_until_the_target_and_keeps_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each result is about 1,000 tokens and its stub about 25. Before call 6
    # the estimate is 1,801 + 1,000 for the unread r4; stubbing r0 and r1
    # brings it to about 850, at or below the 900-token target.
    _set_prune_watermark(monkeypatch, 1_800)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    texts = [str(index) * 3_000 for index in range(5)]
    client = FakeClient(
        [
            *[_respond(_query(f'"{text}"')) for text in texts[:4]],
            _respond(_query(f'"{texts[4]}"'), prompt_tokens=1_801),
            _respond(_query('"r5"')),
            _answer(),
        ]
    )
    capture = SessionCapturePlugin()

    _review(client, plugins=(capture,))

    stub = {"pruned": True, "chars": 3_000, "note": prompts.PRUNED_NOTE}
    full = [{"output": text} for text in texts]
    sent = _list_sent_results(client)
    assert sent[5] == [stub, stub, *full[2:]]
    assert sent[6] == [stub, stub, *full[2:], {"output": "r5"}]
    assert capture.pruned == [0] * 5 + [2, 0]
    kept = [
        response.response
        for event in capture.session.events
        for response in event.get_function_responses()
    ]
    assert kept == [*full, {"output": "r5"}]


def test_pruning_never_stubs_results_the_model_has_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_prune_watermark(monkeypatch, 1_000)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    text = "0" * 300
    client = FakeClient(
        [
            _respond(_query(f'"{text}"')),
            _respond(
                _query(f'"{text}"'),
                _query(f'"{text}"'),
                _query(f'"{text}"'),
                prompt_tokens=1_001,
            ),
            _answer(),
        ]
    )

    _review(client)

    stub = {"pruned": True, "chars": 300, "note": prompts.PRUNED_NOTE}
    assert _list_sent_results(client)[2] == [stub, *[{"output": text}] * 3]


def test_pruning_never_stubs_a_result_smaller_than_its_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_prune_watermark(monkeypatch, 1_000)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    text = "1" * 300
    client = FakeClient(
        [
            _respond(_query('"r0"')),
            _respond(_query(f'"{text}"')),
            _respond(_query('"r2"'), prompt_tokens=1_001),
            _answer(),
        ]
    )

    _review(client)

    stub = {"pruned": True, "chars": 300, "note": prompts.PRUNED_NOTE}
    assert _list_sent_results(client)[3] == [
        {"output": "r0"},
        stub,
        {"output": "r2"},
    ]


def test_new_results_that_push_the_estimate_over_the_watermark_start_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before call 4, the previous prompt is 850 (below the watermark), and the
    # unread r2 adds about 1,000 tokens. Stubbing r0 brings the estimate to
    # about 880, at or below the 900-token target.
    _set_prune_watermark(monkeypatch, 1_800)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    texts = [str(index) * 3_000 for index in range(3)]
    client = FakeClient(
        [
            _respond(_query(f'"{texts[0]}"')),
            _respond(_query(f'"{texts[1]}"')),
            _respond(_query(f'"{texts[2]}"'), prompt_tokens=850),
            _answer(),
        ]
    )

    _review(client)

    stub = {"pruned": True, "chars": 3_000, "note": prompts.PRUNED_NOTE}
    sent = _list_sent_results(client)
    assert sent[2] == [{"output": text} for text in texts[:2]]
    assert sent[3] == [stub, *[{"output": text} for text in texts[1:]]]


def test_the_re_ask_prunes_from_the_first_runs_last_prompt_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_prune_watermark(monkeypatch, 1_800)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    texts = [str(index) * 3_000 for index in range(2)]
    client = FakeClient(
        [
            *[_respond(_query(f'"{text}"')) for text in texts],
            _respond(
                types.Part(text="Prose instead of JSON."), prompt_tokens=1_801
            ),
            _answer([FINDING]),
        ]
    )

    outcome = _review(client)

    stub = {"pruned": True, "chars": 3_000, "note": prompts.PRUNED_NOTE}
    sent = _list_sent_results(client)
    assert sent[2] == [{"output": text} for text in texts]
    assert sent[3] == [stub, {"output": texts[1]}]
    assert outcome.reasked is True


def test_a_prose_answer_is_reasked_once_on_the_same_session() -> None:
    prose = _respond(types.Part(text="The agent refunded a shipped order."))
    client = FakeClient([prose, _answer([FINDING])])

    outcome = _review(client)

    contents, config = client.models.requests[1]
    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].text == "The agent refunded a shipped order."  # type: ignore[index]
    assert contents[2] == types.Content(
        role="user", parts=[types.Part(text=prompts.TEXT_REASK)]
    )
    assert config.tool_config is None
    assert outcome.reasked is True
    assert outcome.parse_path == final_answer.FINAL_PARSE_STRICT
    assert len(outcome.findings) == 1


def test_a_second_prose_answer_raises() -> None:
    prose = _respond(types.Part(text="No JSON here."))
    client = FakeClient([prose, prose])

    with pytest.raises(RuntimeError, match="also after one re-ask") as raised:
        _review(client)

    assert isinstance(raised.value, agentic_reviewer.UnparsableReplyError)
    assert raised.value.final_text == "No JSON here."


def test_production_extract_findings_runs_without_a_plugin() -> None:
    client = FakeClient([_answer([FINDING])])

    findings = agentic_reviewer.extract_findings(
        _build_case(), _build_revisions(), client=client
    )

    assert [finding.agent_id for finding in findings] == [WORKER]


def test_one_client_reviews_sessions_on_separate_event_loops() -> None:
    """Each review runs on its own event loop, so a shared client must serve
    every loop."""
    client = FakeClient([_answer([FINDING]), _answer()])

    first = agentic_reviewer.extract_findings(
        _build_case(), _build_revisions(), client=client
    )
    second = agentic_reviewer.extract_findings(
        _build_case("session-2"), _build_revisions(), client=client
    )

    assert [finding.trajectory_id for finding in first] == ["session-1"]
    assert second == []
    assert len(client.models.requests) == 2


ARGON_INSTRUCTION_SHA256 = (
    "b3f1eb73e5084e6322a3270f55e3e3a5cfea2d73643454f6cee9305f95692345"
)
"""SHA-256 hex digest of the system instruction with `ARGON_*` rules, validated
on gemini-4-argon."""

BASE_INSTRUCTION_SHA256 = (
    "f450da14e603e930e371b7bb1f5f43b3fecf489b14ee9f4ddb062ca72d951469"
)
"""SHA-256 hex digest of the system instruction without `ARGON_*` rules,
validated on claude-opus."""


@pytest.mark.parametrize(
    ("argon_rules", "sha256"),
    [(True, ARGON_INSTRUCTION_SHA256), (False, BASE_INSTRUCTION_SHA256)],
)
def test_each_composed_instruction_is_the_validated_text(
    monkeypatch: pytest.MonkeyPatch, argon_rules: bool, sha256: str
) -> None:
    monkeypatch.setattr(prompts, "ARGON_RULES", argon_rules)

    text = prompts.resolve_system_instruction()

    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == sha256


def test_the_default_review_model_gets_the_argon_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.config import DEFAULT_REVIEW_MODEL

    _use_review_model(monkeypatch, DEFAULT_REVIEW_MODEL)

    text = prompts.resolve_system_instruction()

    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == (
        ARGON_INSTRUCTION_SHA256
    )


def test_the_base_instruction_leaves_out_every_argon_rule() -> None:
    base = prompts.BASE_SYSTEM_INSTRUCTION

    # `str.replace` is a no-op when its target is missing, so verify each
    # insertion anchor appears exactly once in `BASE_SYSTEM_INSTRUCTION`.
    for anchor in (
        prompts._REPORT_ANCHOR,
        prompts._BASE_REPLY_FORMAT,
        prompts._CHECKLIST_ANCHOR,
    ):
        assert base.count(anchor) == 1

    assert prompts.ARGON_JUDGING_RULES not in base
    assert prompts.ARGON_DISMISSED_FIELDS not in base
    assert '"dismissed"' not in base
    assert 'holds only the JSON object {"findings": [...]}.' in base


def test_an_override_replaces_the_composed_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "SYSTEM_INSTRUCTION_OVERRIDE", "Review it.")
    monkeypatch.setattr(prompts, "ARGON_RULES", False)

    assert prompts.resolve_system_instruction() == "Review it."


def test_an_override_reaches_the_prompt_with_the_session_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prompts, "SYSTEM_INSTRUCTION_OVERRIDE", "Review it.\n")

    assert agentic_reviewer.build_review_prompt(
        _build_case(), _build_revisions()
    ).system_instruction == ("Review it.\n" + prompts.UNRECORDED_SCHEMAS_RULE)


def test_each_review_gets_its_own_query_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool holds per-review page origins, so a shared instance would
    let one review continue another review's pages."""
    built: list[JqQueryTool] = []

    class RecordingQueryTool(JqQueryTool):
        def __init__(self, trajectory_json: dict[str, Any]) -> None:
            super().__init__(trajectory_json)
            built.append(self)

    monkeypatch.setattr(
        "ambient_quality_agent.tools.review.agentic_reviewer.tools.jq."
        "JqQueryTool",
        RecordingQueryTool,
    )

    _review(FakeClient([_answer([FINDING])]))
    _review(FakeClient([_answer([FINDING])]))

    assert len(built) == 2
    assert built[0] is not built[1]


@pytest.mark.parametrize(
    ("model", "sha256"),
    [
        ("gemini-4-argon", ARGON_INSTRUCTION_SHA256),
        ("claude-opus-5-5", BASE_INSTRUCTION_SHA256),
    ],
)
def test_unset_argon_rules_follow_the_model_family(
    monkeypatch: pytest.MonkeyPatch, model: str, sha256: str
) -> None:
    _use_review_model(monkeypatch, model)

    text = prompts.resolve_system_instruction()

    assert prompts.ARGON_RULES is None
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == sha256


def test_set_argon_rules_apply_to_a_claude_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_review_model(monkeypatch, "claude-opus-5-5")
    monkeypatch.setattr(prompts, "ARGON_RULES", True)

    text = prompts.resolve_system_instruction()

    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == (
        ARGON_INSTRUCTION_SHA256
    )


def test_the_system_instruction_adds_the_attachment_note_and_focus() -> None:
    attached_case = _build_case()
    attached_case.agent_data.turns[0].events[0].content.parts.append(  # type: ignore[index, union-attr]
        types.Part(file_data=types.FileData(file_uri="gs://b/a"))
    )

    plain = agentic_reviewer.build_review_prompt(
        _build_case(), _build_revisions()
    ).system_instruction
    attached = agentic_reviewer.build_review_prompt(
        attached_case, _build_revisions(), session_review_focus="only refunds"
    ).system_instruction

    assert plain == (
        prompts.resolve_system_instruction() + prompts.UNRECORDED_SCHEMAS_RULE
    )
    assert plain.endswith("unless the instruction names that argument.\n")
    assert attached == (
        prompts.resolve_system_instruction()
        + prompts.UNRECORDED_SCHEMAS_RULE
        + prompts.ATTACHMENT_NOTE
        + prompts.FOCUS_BLOCK.format(focus="only refunds")
    )


def test_recorded_schemas_drop_the_unrecorded_schemas_rule() -> None:
    revisions = [
        AgentRevision(
            agent_id=WORKER,
            instruction="Never refund an order that has shipped.",
            tools=[ToolDefinition(name="lookup", parameters=["order"])],
        )
    ]

    prompt = agentic_reviewer.build_review_prompt(_build_case(), revisions)

    assert prompt.system_instruction == prompts.resolve_system_instruction()


def _render_findings_json(*actuals: str) -> str:
    """Serialize a findings JSON object with one entry per `actuals` string.

    Args:
        *actuals: ``actual_behavior`` descriptions.

    Returns:
        Serialized JSON object string.
    """
    return json.dumps(
        {"findings": [{**FINDING, "actual_behavior": a} for a in actuals]}
    )


@pytest.mark.parametrize(
    ("template", "path"),
    [
        ("OBJECT", "strict"),
        ("```json\nOBJECT\n```", "strict"),
        ("  ```\nOBJECT```\n", "strict"),
        (
            "Here is my review.\n\n```json\nOBJECT\n```\nThat is all.",
            "fenced",
        ),
        (
            'Draft:\n```json\n{"findings": [\n```\nFinal:\n```\nOBJECT\n```\n'
            "Notes:\n```\nnot json\n```",
            "fenced",
        ),
        ("ARRAY", "array"),
        ("Findings:\n```json\nARRAY\n```", "array"),
        ('I checked {"turn": 1} first. Result: OBJECT Done.', "embedded"),
    ],
    ids=[
        "plain",
        "fenced-json",
        "fenced-bare",
        "prose-fence",
        "last-valid-fence",
        "bare-array",
        "fenced-array",
        "embedded",
    ],
)
def test_a_wrapped_text_answer_is_parsed_without_a_re_ask(
    template: str, path: str
) -> None:
    payload = _render_findings_json("a", "b")
    array = json.dumps(json.loads(payload)["findings"])
    answer = template.replace("ARRAY", array).replace("OBJECT", payload)
    client = FakeClient(
        [
            _respond(
                types.Part(text="Thinking about it.", thought=True),
                types.Part(text=answer),
            )
        ]
    )

    outcome = _review(client)

    assert [f.actual_behavior for f in outcome.findings] == ["a", "b"]
    assert len(client.models.requests) == 1
    assert outcome.parse_path == path
    assert outcome.final_text == answer
    assert outcome.reasked is False


def test_a_text_without_findings_json_is_asked_again() -> None:
    client = FakeClient(
        [
            _respond(
                types.Part(
                    text='I found {"defects": 2} and ```\n{"x": 1}\n```.'
                )
            ),
            _respond(
                types.Part(
                    text="Sure:\n```json\n"
                    + _render_findings_json("a")
                    + "\n```"
                )
            ),
        ]
    )

    outcome = _review(client)

    assert [f.actual_behavior for f in outcome.findings] == ["a"]
    assert outcome.reasked is True
    assert outcome.parse_path == final_answer.FINAL_PARSE_FENCED


@pytest.mark.parametrize(
    "second",
    ["still prose", '{"defects": []}', '{"findings": "none"}'],
    ids=str,
)
def test_a_second_answer_without_findings_json_raises(second: str) -> None:
    client = FakeClient(
        [_respond(types.Part(text="prose")), _respond(types.Part(text=second))]
    )

    with pytest.raises(agentic_reviewer.UnparsableReplyError) as raised:
        _review(client)

    assert raised.value.final_text == second
    assert len(client.models.requests) == 2


EXACT_OUTPUT = "a" * 300
"""Output of the exact-request script's first query, large enough to stub."""


def _build_exact_request_script() -> list[types.GenerateContentResponse]:
    """Build a five-call response script exercising all loop controller rules.

    Assumes `MAX_MODEL_CALLS` = 4, a prune watermark of 150 tokens, and
    `KEEP_RECENT_RESULTS` = 1:
    1. Two parallel tool calls with a signed thought.
    2. A tool call that triggers pruning.
    3. A plain tool call.
    4. A forced prose reply.
    5. A forced JSON reply to the re-ask.

    Returns:
        Scripted responses in call order.
    """
    signed = types.Part(
        text="Look at the call.", thought=True, thought_signature=b"sig"
    )
    return [
        _respond(
            signed, _query(f'"{EXACT_OUTPUT}"'), _query(".["), prompt_tokens=200
        ),
        _respond(_query('"b"'), prompt_tokens=200),
        _respond(_query('"c"'), prompt_tokens=100),
        _respond(types.Part(text="Prose instead of JSON.")),
        _answer([FINDING]),
    ]


def _build_text_content(role: str, text: str) -> dict[str, Any]:
    """Build a serialized `Content` dict containing a single text part.

    Args:
        role: Content role (``"user"`` or ``"model"``).
        text: Text payload.

    Returns:
        Dict matching `Content.model_dump` output.
    """
    return {"role": role, "parts": [{"text": text}]}


def _build_call_part(expr: str) -> dict[str, Any]:
    """Build a serialized `query_trajectory` function-call part dict.

    Args:
        expr: jq expression argument.

    Returns:
        Dict matching `Part.model_dump` output.
    """
    return {"function_call": {"name": QUERY_TOOL, "args": {"expr": expr}}}


def _build_response_part(**response: Any) -> dict[str, Any]:
    """Build a serialized `query_trajectory` function-response part dict.

    Args:
        **response: Function response fields.

    Returns:
        Dict matching `Part.model_dump` output.
    """
    return {"function_response": {"name": QUERY_TOOL, "response": response}}


def test_every_request_holds_the_exact_model_visible_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "MAX_MODEL_CALLS", 4)
    _set_prune_watermark(monkeypatch, 150)
    monkeypatch.setattr(agent, "KEEP_RECENT_RESULTS", 1)
    client = FakeClient(_build_exact_request_script())

    outcome = _review(client)

    prompt = agentic_reviewer.build_review_prompt(
        _build_case(), _build_revisions()
    )
    jq_error = (
        "jq compile error: jq: error: syntax error, unexpected end of file at "
        "<top-level>, line 1, column 2:\n    .[\n     ^\njq: 1 compile error"
    )
    note = prompts.PRUNED_NOTE
    first = _build_text_content("user", prompt.initial_message)
    reply_1 = {
        "role": "model",
        "parts": [
            {
                "text": "Look at the call.",
                "thought": True,
                "thought_signature": "c2ln",
            },
            _build_call_part(f'"{EXACT_OUTPUT}"'),
            _build_call_part(".["),
        ],
    }
    results_1 = {
        "role": "user",
        "parts": [
            _build_response_part(output=EXACT_OUTPUT),
            _build_response_part(error=jq_error),
        ],
    }
    pruned_1 = {
        "role": "user",
        "parts": [
            _build_response_part(
                pruned=True, chars=len(EXACT_OUTPUT), note=note
            ),
            _build_response_part(pruned=True, chars=len(jq_error), note=note),
        ],
    }
    reply_2 = {"role": "model", "parts": [_build_call_part('"b"')]}
    results_2 = {"role": "user", "parts": [_build_response_part(output="b")]}
    reply_3 = {"role": "model", "parts": [_build_call_part('"c"')]}
    results_3 = {"role": "user", "parts": [_build_response_part(output="c")]}
    force = _build_text_content(
        "user",
        'Stop using tools. Reply now with only the JSON object {"findings": '
        "[...]}.",
    )
    prose = _build_text_content("model", "Prose instead of JSON.")
    reask = _build_text_content(
        "user",
        'Reply with only the JSON object {"findings": [...]}, with every '
        "defect you found, or an empty list.",
    )
    pruned_history = [first, reply_1, pruned_1, reply_2, results_2]
    assert [
        [
            content.model_dump(mode="json", exclude_none=True)
            for content in contents
        ]
        for contents, _ in client.models.requests
    ] == [
        [first],
        [first, reply_1, results_1],
        pruned_history,
        [*pruned_history, reply_3, results_3, force],
        [*pruned_history, reply_3, results_3, force, prose, reask],
    ]
    forced_config = types.ToolConfig(
        function_calling_config=types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.NONE
        )
    )
    configs = [config for _, config in client.models.requests]
    assert [config.tool_config for config in configs] == [None] * 3 + [
        forced_config
    ] * 2
    for config in configs:
        assert config.system_instruction == (
            prompts.resolve_system_instruction()
            + prompts.UNRECORDED_SCHEMAS_RULE
        )
        assert config.tools == [
            types.Tool(function_declarations=[build_query_declaration()])
        ]
        assert config.thinking_config == types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW, include_thoughts=True
        )
        assert config.automatic_function_calling == (
            types.AutomaticFunctionCallingConfig(disable=True)
        )
        assert config.response_mime_type is None
        assert config.response_schema is None
        assert config.labels == {
            "component": "aqa",
            "adk_agent_name": agent.AGENT_NAME,
        }
    assert [
        finding.model_dump(mode="json") for finding in outcome.findings
    ] == [{**FINDING, "trajectory_id": "session-1", "agent_revision": ""}]
    assert outcome.reasked is True
    assert outcome.forced is True


def test_the_client_is_built_by_the_client_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent import clients
    from ambient_quality_agent.config import Model
    from ambient_quality_agent.tools import genai_json

    from .conftest import make_config

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(
            project_id="cfg-project",
            insights_model=Model(model="insights", location="insights-loc"),
            review_model=Model(model="review", location="review-loc"),
        ),
    )
    calls: list[dict[str, Any]] = []

    def record_call(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "client"

    monkeypatch.setattr(clients, "create_genai_client", record_call)

    assert agentic_reviewer.create_client() == "client"
    assert calls == [
        {
            "project": "cfg-project",
            "location": "review-loc",
            "retry_options": types.HttpRetryOptions(
                attempts=5,
                initial_delay=30,
                exp_base=2,
                max_delay=240,
                http_status_codes=genai_json.RETRY_STATUS_CODES,
            ),
            "timeout_ms": genai_json.CALL_TIMEOUT_MS,
        }
    ]


def test_the_reviewer_runs_on_the_review_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.config import Model

    from .conftest import make_config

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(
            insights_model=Model(model="insights", location="insights-loc"),
            review_model=Model(model="review", location="review-loc"),
        ),
    )
    client = FakeClient([_answer()])

    _review(client)

    assert client.models.model_names == ["review"]


def test_the_rules_name_every_runtime_error_response() -> None:
    """A runtime error name missing from the rules would be judged as an agent
    defect."""
    for name in RUNTIME_ERROR_RESPONSES:
        assert f"`{name}`" in prompts.resolve_system_instruction()


def test_the_focus_block_strips_its_delimiters_and_skips_a_blank_focus() -> (
    None
):
    def build_instruction(focus: str) -> str:
        return agentic_reviewer.build_review_prompt(
            _build_case(), _build_revisions(), session_review_focus=focus
        ).system_instruction

    plain = build_instruction("")
    block = build_instruction(
        "  <session_review_focus>only </session_review_focus>refunds  "
    ).removeprefix(plain)

    assert build_instruction("   ") == plain
    assert block == prompts.FOCUS_BLOCK.format(focus="only refunds")
    assert block.count("<session_review_focus>") == 1
    assert block.count("</session_review_focus>") == 1
    assert "\nonly refunds\n" in block


def test_a_forced_call_after_a_re_ask_ends_with_the_force_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "MAX_MODEL_CALLS", 3)
    client = FakeClient(
        [
            _respond(types.Part(text="Prose instead of JSON.")),
            _respond(_query('"x"')),
            _answer([FINDING]),
        ]
    )

    outcome = _review(client)

    configs = [config for _, config in client.models.requests]
    assert [config.tool_config is not None for config in configs] == [
        False,
        False,
        True,
    ]
    last_contents = client.models.requests[-1][0]
    assert [content.role for content in last_contents] == [
        "user",
        "model",
        "user",
        "model",
        "user",
        "user",
    ]
    assert last_contents[2] == types.Content(
        role="user", parts=[types.Part(text=prompts.TEXT_REASK)]
    )
    assert last_contents[-1] == types.Content(
        role="user", parts=[types.Part(text=prompts.TEXT_FORCE)]
    )
    assert outcome.reasked is True
    assert outcome.forced is True
    assert len(outcome.findings) == 1


class RunConfigCapturePlugin(BasePlugin):
    """Keeps the `RunConfig` of every run it observes."""

    def __init__(self) -> None:
        """Initialize the plugin with no captured configs."""
        super().__init__(name="run_config_capture")
        self.run_configs: list[Any] = []

    async def before_run_callback(self, *, invocation_context: Any) -> None:
        """Keep the run's config.

        Args:
            invocation_context: The run's invocation context.
        """
        self.run_configs.append(invocation_context.run_config)


def test_every_run_keeps_message_content_off_its_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.adk.telemetry import ContentCapturingMode

    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY"
    )
    monkeypatch.setenv(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
    )
    plugin = RunConfigCapturePlugin()
    client = FakeClient([_respond(types.Part(text="prose")), _answer()])

    _review(client, plugins=(plugin,))

    assert len(plugin.run_configs) == 2
    for run_config in plugin.run_configs:
        telemetry = run_config.telemetry
        assert telemetry.capture_message_content is (
            ContentCapturingMode.NO_CONTENT
        )
        assert telemetry.resolved_content_capturing_mode is (
            ContentCapturingMode.NO_CONTENT
        )
        assert not telemetry.should_add_content_to_experimental_spans
        assert not telemetry.should_add_content_to_legacy_spans
        assert run_config.max_llm_calls == agent.MAX_MODEL_CALLS + 2


class CacheConfigCapturePlugin(BasePlugin):
    """Keeps the cache config of every model request it observes."""

    def __init__(self) -> None:
        """Initialize the plugin with no captured configs."""
        super().__init__(name="cache_config_capture")
        self.cache_configs: list[Any] = []

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> None:
        """Keep the request's cache config.

        Args:
            callback_context: The model call's callback context (unused).
            llm_request: The model request.
        """
        del callback_context
        self.cache_configs.append(llm_request.cache_config)


def test_a_gemini_review_sends_no_cache_config() -> None:
    """A cache config would make ADK's `Gemini` create explicit caches."""
    plugin = CacheConfigCapturePlugin()
    client = FakeClient([_respond(_query('"x"')), _answer()])

    _review(client, plugins=(plugin,))

    assert plugin.cache_configs == [None, None]


class BlockingAsyncModels(FakeAsyncModels):
    """Async models whose every call waits until it is cancelled."""

    def __init__(self, models: FakeModels) -> None:
        """Initialize with no calls made.

        Args:
            models: Underlying `FakeModels` instance; unused.
        """
        super().__init__(models)
        self.calls = 0

    async def generate_content(self, **kwargs: Any) -> Any:
        """Wait until cancelled.

        Args:
            **kwargs: Request arguments; unused.

        Returns:
            Never returns.
        """
        self.calls += 1
        await asyncio.Event().wait()


def test_a_review_past_its_deadline_raises_a_deadline_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "REVIEW_DEADLINE_SECONDS", 0.2)
    client = FakeClient([])
    blocking = BlockingAsyncModels(client.models)
    client._fake_aio.models = blocking

    with pytest.raises(ReviewDeadlineError) as raised:
        _review(client)

    assert (
        str(raised.value) == "the review ran longer than 0.2 s and was stopped"
    )
    assert blocking.calls == 1


def test_a_review_past_its_deadline_during_the_re_ask_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent, "REVIEW_DEADLINE_SECONDS", 0.5)
    blocking = BlockingAsyncModels(FakeModels([]))

    class FirstAnswerThenBlock(FakeAsyncModels):
        """Answers the first call with prose, then blocks."""

        async def generate_content(self, **kwargs: Any) -> Any:
            """Answer once, then block.

            Args:
                **kwargs: Request arguments.

            Returns:
                The scripted prose reply.
            """
            if blocking.calls or self._models.requests:
                return await blocking.generate_content(**kwargs)
            return await super().generate_content(**kwargs)

    client = FakeClient([_respond(types.Part(text="prose"))])
    client._fake_aio.models = FirstAnswerThenBlock(client.models)

    with pytest.raises(ReviewDeadlineError):
        _review(client)

    assert len(client.models.requests) == 1
    assert blocking.calls == 1


def test_a_timeout_inside_the_run_is_not_a_deadline_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimingOutModels(FakeAsyncModels):
        """Async models whose call raises its own timeout."""

        async def generate_content(self, **kwargs: Any) -> Any:
            """Raise a timeout.

            Args:
                **kwargs: Request arguments; unused.

            Raises:
                TimeoutError: Always.
            """
            raise TimeoutError("socket timed out")

    client = FakeClient([])
    client._fake_aio.models = TimingOutModels(client.models)

    with pytest.raises(TimeoutError, match="socket timed out") as raised:
        _review(client)

    assert not isinstance(raised.value, ReviewDeadlineError)


def test_the_unparsable_reply_error_names_the_finish_reason() -> None:
    reply = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text="cut off")]
                ),
                finish_reason=types.FinishReason.MAX_TOKENS,
            )
        ]
    )
    client = FakeClient([reply, reply])

    with pytest.raises(agentic_reviewer.UnparsableReplyError) as raised:
        _review(client)

    assert raised.value.finish_reason == "MAX_TOKENS"
    assert "finish_reason=MAX_TOKENS" in str(raised.value)
    assert "error_code=" in str(raised.value)


def test_each_review_logs_one_summary_line_without_session_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ambient_quality_agent.tools.review.agentic_reviewer import reviewer

    client = FakeClient(
        [_respond(_query(".turns | length")), _answer([FINDING])]
    )

    with caplog.at_level(logging.INFO, logger=reviewer.logger.name):
        _review(client)

    (line,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == reviewer.logger.name
    ]
    assert line.startswith(
        "session review finished: trajectory_id=session-1 model_calls=2 "
        "forced=False reasked=False parse_path=strict findings=1 seconds="
    )
    assert "Refund" not in line


def test_a_failed_review_logs_its_summary_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ambient_quality_agent.tools.review.agentic_reviewer import reviewer

    client = FakeClient(
        [_respond(types.Part(text="a")), _respond(types.Part(text="b"))]
    )

    with (
        caplog.at_level(logging.INFO, logger=reviewer.logger.name),
        pytest.raises(agentic_reviewer.UnparsableReplyError),
    ):
        _review(client)

    (line,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == reviewer.logger.name
    ]
    assert line.startswith(
        "session review failed: trajectory_id=session-1 model_calls=2 "
        "forced=False error=UnparsableReplyError seconds="
    )


def test_the_outcome_carries_the_prompt_the_review_sent() -> None:
    client = FakeClient([_answer()])

    outcome = _review(client)

    ((contents, config),) = client.models.requests
    assert outcome.prompt == agentic_reviewer.build_review_prompt(
        _build_case(), _build_revisions()
    )
    assert config.system_instruction == outcome.prompt.system_instruction
    assert contents[0].parts[0].text == outcome.prompt.initial_message  # type: ignore[index]


def test_the_production_client_uses_the_aiohttp_transport() -> None:
    """genai keeps one aiohttp session per event loop, which is what lets the
    concurrent reviews' event loops share one client."""
    client = agentic_reviewer.create_client()

    assert client._api_client._use_aiohttp()


def _use_review_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """Configure `model` at location ``global`` in project ``cfg-project``.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        model: Review model ID.
    """
    from ambient_quality_agent.config import Model

    from .conftest import make_config

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(
            project_id="cfg-project",
            review_model=Model(model=model, location="global"),
        ),
    )


class FakeAnthropicVertex:
    """Stands in for `anthropic.AsyncAnthropicVertex`, answering from a script.

    ADK's `Claude` constructs the client itself, so tests patch the class it
    constructs; `created` keeps every instance.
    """

    created: ClassVar[list[FakeAnthropicVertex]] = []
    script: ClassVar[list[anthropic_types.Message | Exception]] = []

    def __init__(self, *, project_id: str, region: str, **kwargs: Any) -> None:
        """Record the project and region the client is built for.

        Args:
            project_id: Google Cloud project.
            region: Vertex AI region.
            **kwargs: Other client options (unused).
        """
        del kwargs
        self.project_id = project_id
        self.region = region
        self.options: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.messages = self
        FakeAnthropicVertex.created.append(self)

    def with_options(self, **options: Any) -> FakeAnthropicVertex:
        """Record the options and return this client.

        Args:
            **options: Client options such as ``max_retries``.

        Returns:
            This client.
        """
        self.options.update(options)
        return self

    async def create(self, **kwargs: Any) -> anthropic_types.Message:
        """Record a Messages API request and return the next scripted reply.

        Args:
            **kwargs: The request.

        Returns:
            The scripted message.

        Raises:
            Exception: The scripted error, when the next entry is one.
        """
        self.requests.append(copy.deepcopy(kwargs))
        reply = FakeAnthropicVertex.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _build_claude_message(
    *blocks: dict[str, Any], stop_reason: str = "end_turn"
) -> anthropic_types.Message:
    """Build a Claude reply with fixed token usage.

    Args:
        *blocks: Content blocks of the reply.
        stop_reason: Stop reason for the reply.

    Returns:
        `Message` reporting 100 input, 5 cache-read, 20 output, and 7 thinking
        tokens.
    """
    return anthropic_types.Message.model_validate(
        {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(blocks),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "output_tokens_details": {"thinking_tokens": 7},
            },
        }
    )


def _build_claude_query(call_id: str, expr: str) -> dict[str, Any]:
    """Build a `query_trajectory` tool-use block for a Claude reply.

    Args:
        call_id: Tool-use ID.
        expr: jq expression argument.

    Returns:
        Tool-use content block dict.
    """
    return {
        "type": "tool_use",
        "id": call_id,
        "name": QUERY_TOOL,
        "input": {"expr": expr},
    }


_CLAUDE_THINKING = {
    "type": "thinking",
    "thinking": "Check it.",
    "signature": "s1",
}


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> type[FakeAnthropicVertex]:
    """Run the reviewer on claude-opus-5-5 with `FakeAnthropicVertex`.

    Args:
        monkeypatch: The test's monkeypatch fixture.

    Returns:
        The fake client class, whose `script` the test fills.
    """
    from google.adk.models import anthropic_llm

    _use_review_model(monkeypatch, "claude-opus-5-5")
    monkeypatch.setattr(
        anthropic_llm, "AsyncAnthropicVertex", FakeAnthropicVertex
    )
    monkeypatch.setattr(FakeAnthropicVertex, "created", [])
    monkeypatch.setattr(FakeAnthropicVertex, "script", [])
    return FakeAnthropicVertex


def test_a_claude_model_runs_on_vertex_with_adaptive_thinking(
    fake_claude: type[FakeAnthropicVertex],
) -> None:
    fake_claude.script = [
        _build_claude_message(
            _CLAUDE_THINKING,
            _build_claude_query("toolu_1", ".agents | length"),
            stop_reason="tool_use",
        ),
        _build_claude_message(
            {"type": "text", "text": json.dumps({"findings": [FINDING]})}
        ),
    ]
    capture = SessionCapturePlugin()

    outcome = agentic_reviewer.review_session(
        _build_case(), _build_revisions(), plugins=(capture,)
    )

    (client,) = fake_claude.created
    assert (client.project_id, client.region) == ("cfg-project", "global")
    assert client.options == {"max_retries": 0, "timeout": 900.0}
    first, second = client.requests
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["max_tokens"] == agent.CLAUDE_MAX_OUTPUT_TOKENS
    assert first["tool_choice"] == {"type": "auto"}
    assert [tool["name"] for tool in first["tools"]] == [QUERY_TOOL]
    breakpoint_ = {"type": "ephemeral"}
    assert first["system"] == [
        {
            "type": "text",
            "text": (
                prompts.BASE_SYSTEM_INSTRUCTION
                + prompts.UNRECORDED_SCHEMAS_RULE
            ),
            "cache_control": breakpoint_,
        }
    ]
    for request in (first, second):
        assert request["tools"][-1]["cache_control"] == breakpoint_
        assert request["messages"][-1]["content"][-1]["cache_control"] == (
            breakpoint_
        )
    assert second["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Check it.", "signature": "s1"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": QUERY_TOOL,
                "input": {"expr": ".agents | length"},
            },
        ],
    }
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert [finding.actual_behavior for finding in outcome.findings] == [
        FINDING["actual_behavior"]
    ]
    replies = [
        event
        for event in capture.session.events
        if event.author == agent.AGENT_NAME and event.usage_metadata
    ]
    assert [part.text for part in replies[0].content.parts if part.thought] == [
        "Check it."
    ]
    assert [agent.extract_usage(reply) for reply in replies] == [
        {
            "prompt_tokens": 105,
            "output_tokens": 13,
            "thoughts_tokens": 7,
            "cached_tokens": 5,
        }
    ] * 2


def test_a_forced_claude_call_keeps_the_tools_and_chooses_none(
    fake_claude: type[FakeAnthropicVertex], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "MAX_MODEL_CALLS", 2)
    fake_claude.script = [
        _build_claude_message(
            _build_claude_query("toolu_1", '"x"'), stop_reason="tool_use"
        ),
        _build_claude_message({"type": "text", "text": '{"findings": []}'}),
    ]

    outcome = agentic_reviewer.review_session(_build_case(), _build_revisions())

    (client,) = fake_claude.created
    first, forced = client.requests
    assert first["tool_choice"] == {"type": "auto"}
    assert forced["tool_choice"] == {"type": "none"}
    assert [tool["name"] for tool in forced["tools"]] == [QUERY_TOOL]
    assert forced["messages"][-1]["content"][-1] == {
        "type": "text",
        "text": prompts.TEXT_FORCE,
        "cache_control": {"type": "ephemeral"},
    }
    assert outcome.forced is True
    assert outcome.findings == []


@pytest.mark.parametrize(("status", "calls"), [(429, 2), (529, 2), (400, 1)])
def test_only_a_retriable_claude_error_is_retried(
    fake_claude: type[FakeAnthropicVertex],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    calls: int,
) -> None:
    import anthropic
    import httpx2

    monkeypatch.setattr(agent, "QUOTA_RETRY_WAITS_SECONDS", (0,))
    response = httpx2.Response(
        status, request=httpx2.Request("POST", "https://vertex.invalid")
    )
    # The real Vertex client maps each status, so the test sees the error type
    # Vertex raises, such as a generic server error for 529.
    vertex_client = anthropic.AsyncAnthropicVertex(
        region="global", project_id="p", access_token="t"
    )
    retried = calls > 1
    fake_claude.script = [
        vertex_client._make_status_error(
            "refused", body=None, response=response
        ),
        _build_claude_message({"type": "text", "text": '{"findings": []}'}),
    ]

    if retried:
        outcome = agentic_reviewer.review_session(
            _build_case(), _build_revisions()
        )
        assert outcome.findings == []
    else:
        with pytest.raises(anthropic.BadRequestError):
            agentic_reviewer.review_session(_build_case(), _build_revisions())

    (client,) = fake_claude.created
    assert len(client.requests) == calls
