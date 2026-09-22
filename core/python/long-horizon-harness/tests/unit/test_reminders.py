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

"""Pure reminder-builder contract — no LLM assertions, string shape only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.models import LlmRequest
from google.genai import types

from horizon.conversation.reminders import (
    REMINDER_CLOSE,
    REMINDER_OPEN,
    build_budget_reminder,
    build_environment_reminder,
    build_error_reminder,
    build_secrets_reminder,
    build_volatile_reminder,
    make_reminder_injection_callback,
)


class TestBudgetReminder:
    def test_none_when_far_from_limit(self):
        assert build_budget_reminder(iteration=10, max_iterations=50) is None

    def test_warns_when_within_threshold(self):
        text = build_budget_reminder(iteration=46, max_iterations=50)
        assert text is not None
        assert text.startswith(REMINDER_OPEN)
        assert text.endswith(REMINDER_CLOSE)
        assert "4" in text  # remaining iterations
        assert "wrap up" in text.lower() or "running low" in text.lower()

    def test_warns_at_exact_threshold_boundary(self):
        # remaining == warn_at_remaining triggers (inclusive boundary).
        assert (
            build_budget_reminder(
                iteration=45, max_iterations=50, warn_at_remaining=5
            )
            is not None
        )

    def test_none_when_max_iterations_unknown(self):
        assert build_budget_reminder(iteration=5, max_iterations=0) is None


class TestErrorReminder:
    def test_none_when_no_error(self):
        assert build_error_reminder(last_error=None) is None
        assert build_error_reminder(last_error="") is None

    def test_includes_error_text(self):
        text = build_error_reminder(
            last_error="terminal failed: command not found"
        )
        assert text is not None
        assert text.startswith(REMINDER_OPEN)
        assert "terminal failed: command not found" in text
        assert "diagnose" in text.lower()

    def test_truncates_long_error(self):
        text = build_error_reminder(last_error="x" * 5000)
        assert text is not None
        assert len(text) < 1200  # capped, not the full 5000 chars


class TestVolatileReminder:
    def test_none_when_nothing_volatile(self):
        # always_include_date defaults to True; explicitly opt out for the
        # "truly empty" case.
        assert (
            build_volatile_reminder(state={}, always_include_date=False) is None
        )

    def test_includes_iteration_and_date_but_not_error(self):
        from datetime import datetime

        # last_error is surfaced only via build_error_reminder (truncated +
        # guidance); the volatile tail must not duplicate it.
        text = build_volatile_reminder(
            state={"iteration": 7, "last_error": "boom"}
        )
        assert text is not None
        assert text.startswith(REMINDER_OPEN)
        assert "7" in text
        assert "boom" not in text
        assert datetime.now().strftime("%A, %B %d, %Y") in text

    def test_date_present_even_when_state_empty_but_profile_absent(self):
        # Date alone still warrants a reminder so the model always knows "today".
        text = build_volatile_reminder(state={}, always_include_date=True)
        assert text is not None
        assert "today" in text.lower()


class TestEnvironmentReminder:
    def test_wraps_environment_hint_when_present(self, tmp_path):
        text = build_environment_reminder(tmp_path)
        assert text is not None
        assert text.startswith(REMINDER_OPEN) and text.endswith(REMINDER_CLOSE)
        assert str(tmp_path) in text


class TestSecretsReminder:
    @pytest.mark.asyncio
    async def test_none_when_no_user_id(self):
        assert await build_secrets_reminder(None) is None

    @pytest.mark.asyncio
    async def test_none_when_no_secrets(self):
        from horizon.secrets import set_secret_store
        from horizon.secrets.store import SecretManagerStore
        from tests.unit.test_secret_store import FakeSecretClient

        store = SecretManagerStore(client=FakeSecretClient(), project_id="proj")
        set_secret_store(store)
        try:
            assert await build_secrets_reminder("alice@x") is None
        finally:
            set_secret_store(None)

    @pytest.mark.asyncio
    async def test_wraps_the_secrets_line_when_present(self):
        from horizon.secrets import set_secret_store
        from horizon.secrets.store import SecretManagerStore
        from tests.unit.test_secret_store import FakeSecretClient

        store = SecretManagerStore(client=FakeSecretClient(), project_id="proj")
        set_secret_store(store)
        try:
            await store.set_secret("alice@x", "OPENAI_API_KEY", "sk-secret")
            text = await build_secrets_reminder("alice@x")
            assert text is not None
            assert text.startswith(REMINDER_OPEN) and text.endswith(
                REMINDER_CLOSE
            )
            assert "OPENAI_API_KEY" in text
            assert "sk-secret" not in text
        finally:
            set_secret_store(None)


class TestReminderInjectionCallbackIncludesEnvAndSecrets:
    @pytest.mark.asyncio
    async def test_env_hint_rides_the_reminder_tail(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()

        await cb(_ctx({"iteration": 1}), req)

        joined = "\n".join(_reminder_texts(req))
        assert "Working directory:" in joined

    @pytest.mark.asyncio
    async def test_static_instruction_still_untouched_by_env_and_secrets(self):
        # Moving the env hint and secrets line to the tail must not leak
        # them into system_instruction — that would defeat the whole point
        # of excluding volatile content from the context-cache fingerprint.
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        req.config.system_instruction = "STABLE PREFIX"

        await cb(_ctx({"iteration": 1}), req)

        assert req.config.system_instruction == "STABLE PREFIX"


class TestFocusReminder:
    def test_volatile_includes_focus_when_window_set(self):
        from horizon.workspace_window import WORKSPACE_WINDOW_STATE_KEY

        out = build_volatile_reminder(
            state={WORKSPACE_WINDOW_STATE_KEY: ["projA"]}
        )
        assert out is not None and "projA" in out and "focus" in out.lower()

    def test_volatile_no_focus_when_window_unset(self):
        out = build_volatile_reminder(state={"iteration": 1})
        assert out is None or "focus" not in out.lower()


def _ctx(state):
    return SimpleNamespace(state=dict(state))


def _request() -> LlmRequest:
    return LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="hi")]),
        ]
    )


def _reminder_texts(req: LlmRequest) -> list[str]:
    out = []
    for content in req.contents:
        for part in content.parts or []:
            if getattr(part, "text", None) and "<system-reminder>" in part.text:
                out.append(part.text)
    return out


class TestReminderInjectionCallback:
    @pytest.mark.asyncio
    async def test_appends_volatile_reminder_as_trailing_user_content(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        original_len = len(req.contents)

        result = await cb(_ctx({"iteration": 3}), req)

        assert result is None  # never short-circuits
        assert len(req.contents) > original_len
        # Reminders are appended at the TAIL.
        assert "<system-reminder>" in req.contents[-1].parts[0].text
        assert req.contents[-1].role == "user"

    @pytest.mark.asyncio
    async def test_works_with_real_adk_state_object(self):
        # ADK State is not a plain Mapping: dict(State) iterates by index and
        # raises KeyError(0). The callback must handle the real runtime type.
        from google.adk.sessions.state import State

        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        ctx = SimpleNamespace(
            state=State({"iteration": 3, "last_error": "boom"}, {})
        )

        result = await cb(ctx, req)

        assert result is None
        assert "<system-reminder>" in req.contents[-1].parts[0].text

    @pytest.mark.asyncio
    async def test_does_not_touch_system_instruction(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        req.config.system_instruction = "STABLE PREFIX"

        await cb(_ctx({"iteration": 3, "last_error": "boom"}), req)

        # The cached prefix must stay byte-identical.
        assert req.config.system_instruction == "STABLE PREFIX"

    @pytest.mark.asyncio
    async def test_injects_budget_warning_near_limit(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()

        await cb(_ctx({"iteration": 48}), req)

        joined = "\n".join(_reminder_texts(req))
        assert "running low" in joined.lower()

    @pytest.mark.asyncio
    async def test_injects_error_nudge_when_last_error_set(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()

        await cb(_ctx({"last_error": "disk full"}), req)

        joined = "\n".join(_reminder_texts(req))
        assert "disk full" in joined
        assert "diagnose" in joined.lower()

    @pytest.mark.asyncio
    async def test_no_error_reminder_when_no_error(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()

        await cb(_ctx({"iteration": 2}), req)

        joined = "\n".join(_reminder_texts(req))
        assert "diagnose" not in joined.lower()

    @pytest.mark.asyncio
    async def test_skips_when_halt_handoff_delivered(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        before = len(req.contents)
        await cb(
            _ctx({"iteration": 3, "__halt_handoff_delivered__": True}), req
        )
        assert len(req.contents) == before

    @pytest.mark.asyncio
    async def test_skips_when_budget_handoff_delivered(self):
        cb = make_reminder_injection_callback(max_iterations=50)
        req = _request()
        before = len(req.contents)
        await cb(
            _ctx({"iteration": 3, "_iteration_budget_handoff_delivered": True}),
            req,
        )
        assert len(req.contents) == before
