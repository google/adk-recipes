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

"""The reviewer's ADK agent, loop controller, and model client.

Review models starting with `CLAUDE_MODEL_PREFIX` run on ADK's Vertex AI
`Claude` with adaptive thinking at `CLAUDE_EFFORT` and prompt caching, which
`reviewer` enables; all other models run on `Gemini` at `THINKING_LEVEL`.

`ReviewLoopController` applies these steps to each model request:
1. Replaces the system instruction to strip the agent identity line appended by ADK.
2. Stubs the oldest tool results once the estimated next prompt exceeds
   `config.review_prune_watermark_tokens`.
3. Forces the final answer on the last allowed call.
4. Publishes call metadata under ``temp:agentic_review_*`` state keys for observing plugins.
"""

from __future__ import annotations

import asyncio
import json
from functools import cached_property
from typing import TYPE_CHECKING, Any

import anthropic
from ambient_quality_agent.tools import genai_json
from ambient_quality_agent.tools.review.agentic_reviewer import prompts
from google.adk.agents import LlmAgent
from google.adk.models.anthropic_llm import (
    AnthropicGenerateContentConfig,
    Claude,
)
from google.adk.models.google_llm import Gemini
from google.genai import types

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence

    from anthropic import AsyncAnthropicVertex
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
    from google.genai import Client

AGENT_NAME = "session_reviewer"
"""Name of the reviewer agent, and the author of its events."""

CALL_INDEX_STATE_KEY = "temp:agentic_review_call_index"
"""State key with the zero-based index of the model call being made."""

FORCED_STATE_KEY = "temp:agentic_review_forced"
"""State key that is true when the call forces the final answer."""

PRUNED_STATE_KEY = "temp:agentic_review_pruned"
"""State key with the number of tool results first stubbed for this call."""

THINKING_LEVEL = types.ThinkingLevel.LOW
"""Thinking level for every Gemini model call, validated with
`DEFAULT_REVIEW_MODEL`."""

CLAUDE_MODEL_PREFIX = "claude-"
"""Prefix identifying review models that run on ADK's `Claude`."""

CLAUDE_EFFORT = "medium"
"""Adaptive thinking effort for every Claude model call, validated with
claude-opus-5-5.

Adaptive thinking is Anthropic's thinking mode in which the model decides on
each call whether and how much to think; the effort bounds how much."""

CLAUDE_MAX_OUTPUT_TOKENS = 32_000
"""Per-call output token limit for Claude, including thinking tokens.

Anthropic requires an explicit limit, and ADK's 8,192 default can truncate
thinking and findings JSON on long sessions."""

QUOTA_RETRY_WAITS_SECONDS = (30, 60, 120, 240)
"""Backoff delays in seconds for client retries on `genai_json.RETRY_STATUS_CODES`."""

REVIEW_DEADLINE_SECONDS = 1_800
"""Wall-clock limit for one review, the first run and any re-ask together.

Validated runs took a median of 38 s per session and a few minutes at most, so
a review past this limit is stuck rather than slow."""

MAX_MODEL_CALLS = 40
"""Maximum model calls per review; the final call forces a text answer."""

PRUNE_TARGET_FRACTION = 0.5
"""Fraction of the prune watermark that pruning cuts the estimated prompt to.

The gap keeps pruning rare, so earlier content seldom changes and prompt
caching survives."""

CHARS_PER_TOKEN = 3
"""Characters per prompt token used to estimate a tool result's token count.

Measured at about 3.1 characters per prompt token on this reviewer's JSON."""

KEEP_RECENT_RESULTS = 3
"""Number of most recent tool results preserved during pruning."""


def create_client() -> Client:
    """Create a Vertex AI Gemini client at `config.review_model.location`.

    Retries `genai_json.RETRY_STATUS_CODES` with the backoff in
    `QUOTA_RETRY_WAITS_SECONDS` because quota exhaustion windows outlast short
    retries.

    Returns:
        Configured Vertex AI Gemini `Client`.
    """
    from ambient_quality_agent import clients
    from ambient_quality_agent.config import config

    return clients.create_genai_client(
        project=config.project_id,
        location=config.review_model.location,
        retry_options=types.HttpRetryOptions(
            attempts=len(QUOTA_RETRY_WAITS_SECONDS) + 1,
            initial_delay=QUOTA_RETRY_WAITS_SECONDS[0],
            exp_base=2,
            max_delay=QUOTA_RETRY_WAITS_SECONDS[-1],
            http_status_codes=genai_json.RETRY_STATUS_CODES,
        ),
        timeout_ms=genai_json.CALL_TIMEOUT_MS,
    )


def build_reviewer_agent(
    *,
    model: str,
    client: Client | None,
    tools: Sequence[BaseTool],
    controller: ReviewLoopController,
) -> LlmAgent:
    """Build the reviewer `LlmAgent` for a single session.

    Args:
        model: Model ID; a `CLAUDE_MODEL_PREFIX` prefix selects Claude on
            Vertex AI at `config.review_model.location`.
        client: Gemini client for Gemini model calls; None creates one via
            `create_client`. Ignored for Claude because `_ReviewClaude` creates
            a per-review client bound to the active event loop.
        tools: Tools bound to the reviewed session.
        controller: Loop controller for the review.

    Returns:
        Configured `LlmAgent`.
    """
    from ambient_quality_agent.config import config
    from ambient_quality_agent.core.labels import build_request_labels

    no_automatic_calls = types.AutomaticFunctionCallingConfig(disable=True)
    llm: Gemini | Claude
    if model.startswith(CLAUDE_MODEL_PREFIX):
        # `Claude` extracts the project and region from the resource name.
        llm = _ReviewClaude(
            model=(
                f"projects/{config.project_id}/locations/"
                f"{config.review_model.location}/publishers/anthropic/"
                f"models/{model}"
            ),
            max_tokens=CLAUDE_MAX_OUTPUT_TOKENS,
        )
        content_config: types.GenerateContentConfig = (
            AnthropicGenerateContentConfig(
                # A negative budget enables adaptive thinking with summarized
                # thoughts.
                thinking_config=types.ThinkingConfig(thinking_budget=-1),
                effort=CLAUDE_EFFORT,
                automatic_function_calling=no_automatic_calls,
            )
        )
    else:
        llm = Gemini(model=model, client=client or create_client())
        content_config = types.GenerateContentConfig(
            # Request thought summaries so plugins that observe the run can
            # record the reasoning; ADK echoes summary parts back in subsequent
            # turns.
            thinking_config=types.ThinkingConfig(
                thinking_level=THINKING_LEVEL,
                include_thoughts=True,
            ),
            automatic_function_calling=no_automatic_calls,
            labels=build_request_labels(),
        )
    return LlmAgent(
        name=AGENT_NAME,
        model=llm,
        # A callable bypasses ADK's ``{state}`` placeholder injection, which
        # would treat literal braces in the prompt as state keys.
        instruction=lambda context: controller.system_instruction,
        tools=list(tools),
        generate_content_config=content_config,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        before_model_callback=controller.before_model_callback,
        before_tool_callback=controller.before_tool_callback,
    )


def extract_usage(response: Any) -> dict[str, int]:
    """Read token counts from a response's usage metadata.

    Args:
        response: A generate-content or ADK model response.

    Returns:
        ``prompt_tokens``, ``output_tokens``, ``thoughts_tokens`` and
        ``cached_tokens``, zero where unreported.
    """
    usage = getattr(response, "usage_metadata", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
        "output_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
        "thoughts_tokens": int(getattr(usage, "thoughts_token_count", 0) or 0),
        "cached_tokens": int(
            getattr(usage, "cached_content_token_count", 0) or 0
        ),
    }


class _ReviewClaude(Claude):
    """ADK `Claude` configured with the reviewer's retries, timeout, and forced answer."""

    @cached_property
    def _anthropic_client(self) -> AsyncAnthropicVertex:
        """Build the Vertex AI Anthropic client with SDK retries disabled.

        `generate_content_async` handles retries. Setting an explicit timeout
        also allows non-streaming calls to request `CLAUDE_MAX_OUTPUT_TOKENS`,
        which the SDK otherwise rejects as potentially exceeding 10 minutes
        under the default timeout.

        Returns:
            `AsyncAnthropicVertex` client with a per-attempt timeout of
            `genai_json.CALL_TIMEOUT_MS`.
        """
        return super()._anthropic_client.with_options(
            max_retries=0, timeout=genai_json.CALL_TIMEOUT_MS / 1000
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Call Claude using the reviewer's quota retry backoff.

        Retries connection errors, HTTP 529 overloaded errors, and
        `genai_json.RETRY_STATUS_CODES` statuses using
        `QUOTA_RETRY_WAITS_SECONDS`, because quota exhaustion and capacity
        windows outlast the SDK's short backoff. Non-streaming calls yield once
        after completion, so retries never duplicate responses.

        Args:
            llm_request: Model request.
            stream: Whether to stream the response.

        Yields:
            Model responses.
        """
        for wait in (*QUOTA_RETRY_WAITS_SECONDS, None):
            try:
                async for response in super().generate_content_async(
                    llm_request, stream
                ):
                    yield response
                return
            except (
                anthropic.APIConnectionError,
                anthropic.APIStatusError,
            ) as exc:
                # 529 is a transient capacity error, so it gets the same
                # backoff as quota errors. The Vertex client reports it as a
                # generic server error, so match on the status code.
                retriable = (
                    isinstance(exc, anthropic.APIConnectionError)
                    or exc.status_code == 529
                    or exc.status_code in genai_json.RETRY_STATUS_CODES
                )
                if wait is None or not retriable:
                    raise
            await asyncio.sleep(wait)

    def _build_anthropic_kwargs(
        self,
        llm_request: LlmRequest,
        messages: Any,
        tools: Any,
        tool_choice: Any,
        thinking: Any,
    ) -> dict[str, Any]:
        """Build Messages API arguments, enforcing forced final answers.

        ADK's `Claude` ignores ``tool_config``, so when function calling is
        disabled, this sets ``tool_choice`` to ``none`` while keeping `tools`
        declared so prior tool calls and results in `messages` remain valid.

        Args:
            llm_request: Model request.
            messages: Serialized conversation messages.
            tools: Tool definitions, or `NotGiven`.
            tool_choice: Tool choice selected by `Claude`, or `NotGiven`.
            thinking: Thinking configuration, or `NotGiven`.

        Returns:
            Keyword arguments for ``messages.create``.
        """
        kwargs = super()._build_anthropic_kwargs(
            llm_request, messages, tools, tool_choice, thinking
        )
        tool_config = llm_request.config.tool_config
        calling = tool_config.function_calling_config if tool_config else None
        if calling and calling.mode == types.FunctionCallingConfigMode.NONE:
            kwargs["tool_choice"] = {"type": "none"}
        return kwargs


class ReviewLoopController:
    """Controls the model and tool calls of a single session review.

    A single instance spans the initial run and any re-ask so the re-ask counts
    toward `MAX_MODEL_CALLS` and pruning state.
    """

    def __init__(
        self,
        system_instruction: str,
        tool_names: Sequence[str],
        prune_watermark_tokens: int,
    ) -> None:
        """Initialize the loop controller.

        Args:
            system_instruction: System instruction applied to every model call.
            tool_names: Names of the tools the agent declares; a call to any
                other name gets an error result.
            prune_watermark_tokens: Estimated next prompt size in tokens above
                which old tool results are stubbed; see
                `_update_pruned_results`.
        """
        self._system_instruction = system_instruction
        self._tool_names = tuple(tool_names)
        self._prune_watermark_tokens = prune_watermark_tokens
        self._prune_target_tokens = int(
            prune_watermark_tokens * PRUNE_TARGET_FRACTION
        )
        self._call_index = 0
        self._last_call_forced = False
        # Zero-based ordinals of stubbed function responses across the conversation.
        self._pruned: set[int] = set()
        # Index where the forced-answer prompt was inserted; reused on subsequent
        # calls because the request history is append-only.
        self._force_position: int | None = None

    @property
    def system_instruction(self) -> str:
        """System instruction applied to every model call."""
        return self._system_instruction

    @property
    def last_call_forced(self) -> bool:
        """True if the most recent model call forced the final answer."""
        return self._last_call_forced

    @property
    def call_count(self) -> int:
        """Number of model calls made so far in the review."""
        return self._call_index

    def before_model_callback(
        self, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        """Adjust `llm_request` before each model call.

        1. Replace the system instruction to strip the agent identity line
           appended by ADK.
        2. Stub the oldest tool results once the estimated prompt exceeds the
           prune watermark.
        3. On the last allowed call, disable function calling and insert
           `prompts.TEXT_FORCE`.
        4. Publish the call index, forced flag, and newly stubbed count under
           ``temp:agentic_review_*`` state keys.

        Args:
            callback_context: ADK callback context for the model call.
            llm_request: Model request modified in place.

        Returns:
            None, allowing the model call to proceed.
        """
        call_index = self._call_index
        self._call_index += 1
        llm_request.config.system_instruction = self._system_instruction
        previous_prompt_tokens = next(
            (
                event.usage_metadata.prompt_token_count
                for event in reversed(callback_context.session.events)
                if event.usage_metadata is not None
                and event.usage_metadata.prompt_token_count is not None
            ),
            0,
        )
        newly_pruned = self._update_pruned_results(
            llm_request.contents, previous_prompt_tokens
        )
        llm_request.contents = self._build_pruned_contents(llm_request.contents)
        forced = call_index >= MAX_MODEL_CALLS - 1
        if forced:
            llm_request.config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.NONE
                )
            )
            if self._force_position is None:
                self._force_position = len(llm_request.contents)
            llm_request.contents.insert(
                self._force_position,
                types.Content(
                    role="user", parts=[types.Part(text=prompts.TEXT_FORCE)]
                ),
            )
        self._last_call_forced = forced
        callback_context.state[CALL_INDEX_STATE_KEY] = call_index
        callback_context.state[FORCED_STATE_KEY] = forced
        callback_context.state[PRUNED_STATE_KEY] = newly_pruned
        return None

    def before_tool_callback(
        self, tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, str] | None:
        """Reject calls to undeclared tools with an error dict.

        Args:
            tool: Target tool (a placeholder when the tool name is undeclared).
            args: Tool call arguments (unused).
            tool_context: ADK tool execution context (unused).

        Returns:
            ``{"error": text}`` if `tool.name` is not a declared tool, or None
            to proceed with execution.
        """
        del args, tool_context
        if tool.name in self._tool_names:
            return None
        return {
            "error": prompts.UNKNOWN_TOOL_ERROR.format(
                name=tool.name, tool_names=", ".join(self._tool_names)
            )
        }

    def _update_pruned_results(
        self, contents: Sequence[types.Content], previous_prompt_tokens: int
    ) -> int:
        """Mark the oldest tool results for pruning when the prompt is too large.

        1. Estimate the next prompt as `previous_prompt_tokens` plus the
           results added after the last model turn.
        2. Skip if the estimate is at or below the watermark.
        3. Mark the oldest unpruned results until the estimate is at or below
           `PRUNE_TARGET_FRACTION` of the watermark, or until none remain.
           The newest `KEEP_RECENT_RESULTS` results, results the model has
           not seen yet, and results no larger than their stub are preserved.

        An underestimate is caught on the next call, which checks the actual
        prompt size.

        Args:
            contents: Request contents before stubbing.
            previous_prompt_tokens: Prompt token count reported for the most
                recent model call, or 0 before the first call.

        Returns:
            Number of tool results newly marked as pruned.
        """
        responses: list[types.FunctionResponse] = []
        unseen = 0
        for content in contents:
            if content.role == "model":
                unseen = 0
            for part in content.parts or []:
                if part.function_response is not None:
                    responses.append(part.function_response)
                    unseen += 1
        estimate = previous_prompt_tokens + sum(
            _estimate_response_tokens(response)
            for response in responses[len(responses) - unseen :]
        )
        if estimate <= self._prune_watermark_tokens:
            return 0
        cutoff = len(responses) - max(KEEP_RECENT_RESULTS, unseen)
        added = 0
        for ordinal in range(cutoff):
            if estimate <= self._prune_target_tokens:
                break
            if ordinal in self._pruned:
                continue
            response = responses[ordinal]
            saved = _estimate_response_tokens(
                response
            ) - _estimate_response_tokens(_build_pruned_response(response))
            if saved <= 0:
                continue
            estimate -= saved
            self._pruned.add(ordinal)
            added += 1
        return added

    def _build_pruned_contents(
        self, contents: Sequence[types.Content]
    ) -> list[types.Content]:
        """Replace every pruned function response with a stub.

        Copies modified `Content` and `Part` objects because ADK request
        contents share part payloads with session events, preserving the full
        tool results in the session history.

        Args:
            contents: The request contents.

        Returns:
            Request contents with pruned function responses replaced by stubs.
        """
        ordinal = 0
        rebuilt: list[types.Content] = []
        for content in contents:
            parts: list[types.Part] = []
            changed = False
            for part in content.parts or []:
                response = part.function_response
                if response is None:
                    parts.append(part)
                    continue
                if ordinal in self._pruned:
                    parts.append(
                        types.Part(
                            function_response=_build_pruned_response(response)
                        )
                    )
                    changed = True
                else:
                    parts.append(part)
                ordinal += 1
            rebuilt.append(
                content.model_copy(update={"parts": parts})
                if changed
                else content
            )
        return rebuilt


def _build_pruned_response(
    response: types.FunctionResponse,
) -> types.FunctionResponse:
    """Build the stub that replaces a pruned tool result.

    Args:
        response: The original function response.

    Returns:
        A response to the same call with the size of the original output or
        error text and `prompts.PRUNED_NOTE`.
    """
    body = response.response or {}
    text = body.get("output", body.get("error", ""))
    return types.FunctionResponse(
        name=response.name,
        id=response.id,
        response={
            "pruned": True,
            "chars": len(str(text)),
            "note": prompts.PRUNED_NOTE,
        },
    )


def _estimate_response_tokens(response: types.FunctionResponse) -> int:
    """Estimate the prompt tokens of a tool result from its JSON size.

    Args:
        response: The function response.

    Returns:
        Estimated token count at `CHARS_PER_TOKEN` characters per token.
    """
    text = json.dumps(response.response or {}, ensure_ascii=False, default=str)
    return len(text) // CHARS_PER_TOKEN
