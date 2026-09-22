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

"""Synthetic <system-reminder> builders for the rolling message tail.

These produce small, individually-tagged reminder strings that a
before_model_callback appends to llm_request.contents (NOT the cached
system_instruction). Keeping them out of the system prefix means the
stable prefix stays byte-identical across turns, so the model's
prompt/context cache keeps hitting; per-turn steering rides in the
tail where re-caching is cheap.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.genai import types

from horizon.conversation.iteration_budget_plugin import (
    _DEFAULT_MAX_ITERATIONS as _PLUGIN_DEFAULT_MAX_ITERATIONS,
)
from horizon.memory.user_profile import render_user_profile
from horizon.workspace_window import render_window, window_dirs

REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"

_DATE_FORMAT = "%A, %B %d, %Y"
_DEFAULT_WARN_AT_REMAINING = 5
_MAX_ERROR_CHARS = 800

# Imported, not duplicated: the two 50s used to agree by coincidence (no
# shared constant), so a future change to either default would silently
# desync this warning from the plugin's real threshold.
_DEFAULT_MAX_ITERATIONS = _PLUGIN_DEFAULT_MAX_ITERATIONS

# Handoff flags owned by the two halt consumers (guardrails + budget). Held as
# literals so this hot-path module stays import-light; kept in sync with
# horizon/guardrails/halt_consumer.py and horizon/conversation/iteration_budget_plugin.py.
_GUARDRAILS_HANDOFF_DELIVERED_STATE_KEY = "__halt_handoff_delivered__"
_BUDGET_HANDOFF_DELIVERED_STATE_KEY = "_iteration_budget_handoff_delivered"


def _wrap(body: str) -> str:
    return f"{REMINDER_OPEN}\n{body.strip()}\n{REMINDER_CLOSE}"


def build_budget_reminder(
    *,
    iteration: int,
    max_iterations: int,
    warn_at_remaining: int = _DEFAULT_WARN_AT_REMAINING,
) -> str | None:
    if max_iterations <= 0:
        return None
    remaining = max_iterations - iteration
    if remaining > warn_at_remaining:
        return None
    remaining = max(remaining, 0)
    return _wrap(
        f"Iteration budget running low: {remaining} of {max_iterations} "
        "iterations remain before this turn is force-stopped. Wrap up — "
        "finish the current step, then deliver your result rather than "
        "starting new long-running work."
    )


def build_error_reminder(*, last_error: str | None) -> str | None:
    if not last_error:
        return None
    text = str(last_error)
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + " […truncated]"
    return _wrap(
        f"Your last tool call failed: {text}\n"
        "Before retrying, diagnose the cause — read the failing file or its "
        "test, check what changed. Do not re-run the same call with a tweaked "
        "flag; that is a retry, not a fix."
    )


def build_environment_reminder(cwd: str | Path | None = None) -> str | None:
    """Env/workspace hint, moved here from the cached system_instruction.

    Genuinely volatile (cwd, local CLI probes) and small, so it rides the
    trailing <system-reminder> tail — excluded from the context-cache
    fingerprint (_find_count_of_contents_to_cache) rather than invalidating
    the cached static prefix when it changes.
    """
    from horizon.conversation.system_prompt import (
        _default_cwd_for_hint,
        build_environment_hints,
    )

    hint = build_environment_hints(
        cwd if cwd is not None else _default_cwd_for_hint()
    )
    if not hint:
        return None
    return _wrap(hint)


async def build_secrets_reminder(user_id: str | None) -> str | None:
    """Available-secrets line, moved here so a mid-session Connect-Google
    does not invalidate the static_instruction context-cache fingerprint."""
    from horizon.conversation.system_prompt import _available_secrets_line

    line = await _available_secrets_line(user_id)
    if not line:
        return None
    return _wrap(line)


def build_volatile_reminder(
    *,
    state: dict[str, Any] | None = None,
    always_include_date: bool = True,
) -> str | None:
    state = state or {}
    lines: list[str] = []

    profile = render_user_profile(state)
    if profile:
        lines.append(profile)

    focus = render_window(window_dirs(state))
    if focus:
        lines.append(focus)

    iteration = state.get("iteration") or 0
    if iteration:
        lines.append(f"Iteration: {iteration}")

    # last_error is surfaced once, via build_error_reminder (truncated + diagnosis
    # guidance); don't duplicate the (untruncated) error in the volatile tail.
    if always_include_date or lines:
        lines.append(f"Today is {datetime.now().strftime(_DATE_FORMAT)}.")

    if not lines:
        return None
    return _wrap("\n\n".join(lines))


def _append_reminder(llm_request: LlmRequest, text: str) -> None:
    llm_request.contents.append(
        types.Content(role="user", parts=[types.Part(text=text)])
    )


def make_reminder_injection_callback(
    *,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
) -> Callable[[CallbackContext, LlmRequest], Awaitable[LlmResponse | None]]:
    """before_model_callback that appends <system-reminder> Content to the tail.

    Never short-circuits (always returns None). Volatile state, the
    near-budget warning, and the last-error nudge ride in the rolling
    message tail so the cached system prefix stays stable.
    """

    async def _callback(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        state_obj = getattr(callback_context, "state", None)
        # ADK State is not a plain Mapping: dict(State) falls back to sequence
        # iteration and raises KeyError(0). Use its to_dict() snapshot.
        if hasattr(state_obj, "to_dict"):
            state = state_obj.to_dict()
        else:
            state = dict(state_obj or {})

        if state.get(_GUARDRAILS_HANDOFF_DELIVERED_STATE_KEY) or state.get(
            _BUDGET_HANDOFF_DELIVERED_STATE_KEY
        ):
            return None

        volatile = build_volatile_reminder(state=state)
        if volatile:
            _append_reminder(llm_request, volatile)

        env_hint = build_environment_reminder()
        if env_hint:
            _append_reminder(llm_request, env_hint)

        secrets = await build_secrets_reminder(
            getattr(callback_context, "user_id", None)
        )
        if secrets:
            _append_reminder(llm_request, secrets)

        budget = build_budget_reminder(
            iteration=int(state.get("iteration") or 0),
            max_iterations=max_iterations,
        )
        if budget:
            _append_reminder(llm_request, budget)

        error = build_error_reminder(last_error=state.get("last_error"))
        if error:
            _append_reminder(llm_request, error)

        return None

    return _callback


reminder_injection_callback = make_reminder_injection_callback()


__all__ = [
    "REMINDER_CLOSE",
    "REMINDER_OPEN",
    "build_budget_reminder",
    "build_environment_reminder",
    "build_error_reminder",
    "build_secrets_reminder",
    "build_volatile_reminder",
    "make_reminder_injection_callback",
    "reminder_injection_callback",
]
