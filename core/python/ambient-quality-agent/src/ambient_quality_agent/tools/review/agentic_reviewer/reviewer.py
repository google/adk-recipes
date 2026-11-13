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

"""Reviews a single session using an ADK agent loop.

`review_session`:
1. Builds the review prompt and the `query_trajectory` jq tool.
2. Runs the agent until its first reply without a function call.
3. Re-asks once if that reply contains no findings JSON.
4. Parses and validates the findings.
5. Logs one summary line with counts and timing, and no session content.

Steps 2 and 3 together must finish within `agent.REVIEW_DEADLINE_SECONDS`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.review.agentic_reviewer import (
    agent,
    final_answer,
    prompts,
)
from ambient_quality_agent.tools.review.agentic_reviewer.tools import jq
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.agents.run_config import RunConfig
from google.adk.apps import App
from google.adk.models.anthropic_llm import Claude
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.telemetry import ContentCapturingMode, TelemetryConfig
from google.genai import types

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.agent_revision import AgentRevision
    from ambient_quality_agent.tools.insights.findings import Finding
    from google.adk.agents import LlmAgent
    from google.adk.events.event import Event
    from google.adk.plugins.base_plugin import BasePlugin
    from google.genai import Client

logger = logging.getLogger(__name__)

_APP_NAME = "agentic_session_review"
_USER_ID = "agentic-session-review"


@dataclasses.dataclass(frozen=True)
class ReviewOutcome:
    """Result of a single session review."""

    findings: list[Finding]
    """Validated findings, capped at `findings.MAX_FINDINGS_PER_TRAJECTORY`."""

    final_text: str
    """Text of the final reply from which `findings` were parsed."""

    parse_path: str
    """`final_answer.FINAL_PARSE_*` strategy that extracted the findings JSON."""

    reasked: bool
    """True if the first final reply failed to parse and `prompts.TEXT_REASK` was sent."""

    forced: bool
    """True if the call producing `final_text` forced a final answer."""

    prompt: prompts.ReviewPrompt
    """The prompt the review sent."""


@dataclasses.dataclass(frozen=True)
class _FinalReply:
    """The agent's final reply in one run."""

    text: str
    """Non-thought text of the reply; empty if it has none."""

    finish_reason: str
    """The reply's finish reason; empty if unreported."""

    error_code: str
    """The reply's error code; empty if none."""


class UnparsableReplyError(RuntimeError):
    """Raised when the final reply contains no findings JSON, even after one re-ask."""

    def __init__(
        self, final_text: str, *, finish_reason: str = "", error_code: str = ""
    ) -> None:
        """Initialize the error.

        Args:
            final_text: Text of the reply to the re-ask.
            finish_reason: The reply's finish reason; empty if unreported.
            error_code: The reply's error code; empty if none.
        """
        super().__init__(
            "the reviewer's final text was not a JSON object with a findings "
            "list, also after one re-ask (finish_reason="
            f"{finish_reason or 'none'}, error_code={error_code or 'none'})"
        )
        self.final_text = final_text
        self.finish_reason = finish_reason
        self.error_code = error_code


class ReviewDeadlineError(TimeoutError):
    """Raised when a review runs past `agent.REVIEW_DEADLINE_SECONDS`."""

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "the review ran longer than "
            f"{agent.REVIEW_DEADLINE_SECONDS} s and was stopped"
        )


def extract_findings(
    eval_case: Any,
    revisions: Sequence[AgentRevision],
    *,
    session_review_focus: str = "",
    goal: str = "",
    client: Client | None = None,
    plugins: Sequence[BasePlugin] = (),
) -> list[Finding]:
    """Review a session on the ADK agent loop and return its findings.

    Use it where only the findings matter; `review_session` also returns the
    reply metadata.

    Args:
        eval_case: Evaluation case containing the conversation turns and
            session ID.
        revisions: Agent definitions active during the session.
        session_review_focus: Optional scope to filter reported findings. An
            empty string reviews for all defects.
        goal: Developer context about the agents and deployment for the
            Additional information section; empty omits the section.
        client: Gemini client; None creates one with `agent.create_client`.
            Claude review models ignore it.
        plugins: ADK plugins that observe the run.

    Returns:
        Up to `findings.MAX_FINDINGS_PER_TRAJECTORY` findings.

    Raises:
        UnparsableReplyError: If the model returns no parsable findings JSON
            even after one re-ask.
        ReviewDeadlineError: If the review exceeds
            `agent.REVIEW_DEADLINE_SECONDS`.
        Exception: Propagates model and HTTP failures so the caller can
            record the case as errored.
    """
    return review_session(
        eval_case,
        revisions,
        session_review_focus=session_review_focus,
        goal=goal,
        client=client,
        plugins=plugins,
    ).findings


def review_session(
    eval_case: Any,
    revisions: Sequence[AgentRevision],
    *,
    session_review_focus: str = "",
    goal: str = "",
    client: Client | None = None,
    plugins: Sequence[BasePlugin] = (),
) -> ReviewOutcome:
    """Review a session on the ADK agent loop.

    Runs synchronously on a dedicated event loop so callers can dispatch
    session reviews from a thread pool.

    Args:
        eval_case: Evaluation case containing the conversation turns and
            session ID.
        revisions: Agent definitions active during the session.
        session_review_focus: Optional scope to filter reported findings. An
            empty string reviews for all defects.
        goal: Developer context about the agents and deployment; empty omits
            the Additional information section.
        client: Gemini client; None creates one with `agent.create_client`.
            Claude review models ignore it.
        plugins: ADK plugins that observe the run.

    Returns:
        The extracted findings and final reply metadata.

    Raises:
        UnparsableReplyError: If the model returns no parsable findings JSON
            even after one re-ask.
        ReviewDeadlineError: If the review exceeds
            `agent.REVIEW_DEADLINE_SECONDS`.
    """
    from ambient_quality_agent.config import config

    trajectory_id = str(getattr(eval_case, "eval_case_id", "") or "")
    started = time.monotonic()
    controller: agent.ReviewLoopController | None = None
    try:
        prompt = prompts.build_review_prompt(
            eval_case,
            revisions,
            session_review_focus=session_review_focus,
            goal=goal,
        )
        # Each review needs its own tool, which holds its jq page origins.
        query_tool = jq.JqQueryTool(prompt.trajectory_json)
        controller = agent.ReviewLoopController(
            prompt.system_instruction,
            [query_tool.name],
            prune_watermark_tokens=config.review_prune_watermark_tokens,
        )
        reviewer_agent = agent.build_reviewer_agent(
            model=config.review_model.model,
            client=client,
            tools=[query_tool],
            controller=controller,
        )
        outcome = asyncio.run(
            _run_review(
                reviewer_agent,
                controller,
                prompt,
                trajectory_id=trajectory_id,
                known_agent_ids={revision.agent_id for revision in revisions},
                plugins=plugins,
            )
        )
    except Exception as exc:
        _log_review_summary(
            trajectory_id, controller, time.monotonic() - started, error=exc
        )
        raise
    _log_review_summary(
        trajectory_id, controller, time.monotonic() - started, outcome=outcome
    )
    return outcome


def _log_review_summary(
    trajectory_id: str,
    controller: agent.ReviewLoopController | None,
    seconds: float,
    *,
    outcome: ReviewOutcome | None = None,
    error: Exception | None = None,
) -> None:
    """Log one INFO line with a review's counts and timing.

    The line holds no session content, so production logs can carry it.

    Args:
        trajectory_id: The reviewed trajectory's ID.
        controller: The review's loop controller, which counts its model
            calls; None if the review failed before creating it.
        seconds: Wall-clock duration of the review.
        outcome: The review's outcome; None if it failed.
        error: The exception that failed the review; None if it succeeded.
    """
    model_calls = controller.call_count if controller is not None else 0
    if outcome is None:
        logger.info(
            "session review failed: trajectory_id=%s model_calls=%d "
            "forced=%s error=%s seconds=%.1f",
            trajectory_id,
            model_calls,
            controller is not None and controller.last_call_forced,
            type(error).__name__,
            seconds,
        )
        return
    logger.info(
        "session review finished: trajectory_id=%s model_calls=%d forced=%s "
        "reasked=%s parse_path=%s findings=%d seconds=%.1f",
        trajectory_id,
        model_calls,
        outcome.forced,
        outcome.reasked,
        outcome.parse_path,
        len(outcome.findings),
        seconds,
    )


async def _run_review(
    reviewer_agent: LlmAgent,
    controller: agent.ReviewLoopController,
    prompt: prompts.ReviewPrompt,
    *,
    trajectory_id: str,
    known_agent_ids: set[str],
    plugins: Sequence[BasePlugin],
) -> ReviewOutcome:
    """Run the reviewer agent until its final reply parses, re-asking at most once.

    Args:
        reviewer_agent: Configured reviewer agent.
        controller: Loop controller governing the review.
        prompt: The review's prompt; its initial message starts the run.
        trajectory_id: Trajectory identifier for finding attribution.
        known_agent_ids: Valid agent identifiers for the session.
        plugins: ADK plugins observing the run.

    Returns:
        Completed `ReviewOutcome`.

    Raises:
        UnparsableReplyError: If neither the initial final reply nor the re-ask
            reply contains findings JSON.
        ReviewDeadlineError: If the runs take longer than
            `agent.REVIEW_DEADLINE_SECONDS` together.
    """
    deadline = asyncio.timeout(agent.REVIEW_DEADLINE_SECONDS)
    try:
        async with (
            deadline,
            Runner(
                app=App(
                    name=_APP_NAME,
                    root_agent=reviewer_agent,
                    plugins=list(plugins),
                    # Claude caches the prompt only at the breakpoints this
                    # enables. The default TTL, under an hour, selects its
                    # 5-minute cache, which outlasts the gap between the calls
                    # of one review. Gemini caches implicitly, and this config
                    # would make it create explicit cache resources.
                    context_cache_config=(
                        ContextCacheConfig()
                        if isinstance(reviewer_agent.model, Claude)
                        else None
                    ),
                ),
                session_service=InMemorySessionService(),
            ) as runner,
        ):
            session = await runner.session_service.create_session(
                app_name=_APP_NAME, user_id=_USER_ID
            )
            reply = await _request_final_reply(
                runner, session.id, prompt.initial_message
            )
            parsed = final_answer.parse_final_text(
                reply.text, trajectory_id, known_agent_ids
            )
            reasked = parsed is None
            if parsed is None:
                reply = await _request_final_reply(
                    runner, session.id, prompts.TEXT_REASK
                )
                parsed = final_answer.parse_final_text(
                    reply.text, trajectory_id, known_agent_ids
                )
                if parsed is None:
                    raise UnparsableReplyError(
                        reply.text,
                        finish_reason=reply.finish_reason,
                        error_code=reply.error_code,
                    )
    except TimeoutError as exc:
        # Raise `ReviewDeadlineError` only when `deadline` itself expired;
        # propagate inner timeouts unchanged.
        if not deadline.expired():
            raise
        raise ReviewDeadlineError() from exc
    findings, parse_path = parsed
    return ReviewOutcome(
        findings=findings,
        final_text=reply.text,
        parse_path=parse_path,
        reasked=reasked,
        forced=controller.last_call_forced,
        prompt=prompt,
    )


async def _request_final_reply(
    runner: Runner, session_id: str, message: str
) -> _FinalReply:
    """Send a user message and run the agent until it replies without a tool call.

    Args:
        runner: ADK runner for the review.
        session_id: ADK session ID to continue.
        message: User message text.

    Returns:
        The agent's final reply without a function call: its non-thought text,
        empty if it has none, and its finish reason and error code.
    """
    final_event: Event | None = None
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part(text=message)]
        ),
        # Keep `max_llm_calls` above `agent.MAX_MODEL_CALLS` so
        # `ReviewLoopController` forces a final answer before ADK aborts the
        # loop. Omit message content from telemetry spans because reviewer
        # messages contain whole sessions (`ADK_TELEMETRY_IGNORE_RUN_CONFIG`
        # overrides this).
        run_config=RunConfig(
            max_llm_calls=agent.MAX_MODEL_CALLS + 2,
            telemetry=TelemetryConfig(
                capture_message_content=ContentCapturingMode.NO_CONTENT
            ),
        ),
    ):
        if (
            event.author == agent.AGENT_NAME
            and not event.partial
            and not event.get_function_calls()
            and not event.get_function_responses()
        ):
            final_event = event
    if final_event is None:
        return _FinalReply(text="", finish_reason="", error_code="")
    return _FinalReply(
        text=final_answer.extract_reply_text(final_event.content),
        finish_reason=_format_reason(final_event.finish_reason),
        error_code=_format_reason(final_event.error_code),
    )


def _format_reason(value: Any) -> str:
    """Format a finish reason or error code for an error message.

    Args:
        value: An enum member, such as `types.FinishReason`, a string, or None.

    Returns:
        The member's name, the string itself, or an empty string for None.
    """
    if value is None:
        return ""
    return str(getattr(value, "name", value))
