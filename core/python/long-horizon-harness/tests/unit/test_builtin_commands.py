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

"""Built-in slash commands (``/dream-review``, ``/reload``, ``/model``, ``/grant``).

These cover capabilities that belong to the user's affordance surface, not
the model's. The model never sees ``/cmd`` turns — they're intercepted by
``make_slash_command_dispatcher`` in ``horizon/commands/dispatcher.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.types import Content, Part

pytestmark = pytest.mark.asyncio


def _llm_request_with_text(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        contents=[Content(role="user", parts=[Part(text=text)])],
    )


def _callback_ctx(
    *,
    state: dict,
    memory_service=None,
    app_name: str = "test_app",
    user_id: str = "test-user",
) -> SimpleNamespace:
    invocation_context = SimpleNamespace(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
    )
    return SimpleNamespace(
        state=state,
        user_id=user_id,
        _invocation_context=invocation_context,
    )


async def test_module_registers_builtins():
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    for name in ("dream-review", "reload", "grant", "model"):
        assert name in BUILTIN_COMMAND_REGISTRY


# =============================================================================
# /dream-review
# =============================================================================


async def test_dream_review_calls_internal_entrypoint(monkeypatch):
    from horizon.commands import BUILTIN_COMMAND_REGISTRY
    from horizon.memory import dream_review

    spy = AsyncMock(return_value={"success": True, "sessions_reviewed": 3})
    monkeypatch.setattr(dream_review, "_run_dream_review_for_user", spy)

    handler = BUILTIN_COMMAND_REGISTRY["dream-review"]
    ctx = _callback_ctx(state={}, memory_service=object(), user_id="ada")
    out = await handler("", ctx)

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["user_id"] == "ada"
    assert kwargs["app_name"] == "test_app"
    assert "sessions_reviewed" in out or "3" in out


async def test_dream_review_handles_missing_invocation_context():
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    handler = BUILTIN_COMMAND_REGISTRY["dream-review"]
    ctx = SimpleNamespace(state={}, user_id="ada", _invocation_context=None)
    out = await handler("", ctx)

    assert "invocation" in out.lower() or "context" in out.lower()


# =============================================================================
# /reload — refresh the skill catalog
# =============================================================================


async def test_reload_returns_noop_when_no_toolset_bound(monkeypatch):
    import horizon.commands as commands_mod
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    async def _noop():
        return None

    monkeypatch.setattr(commands_mod, "resync_and_refresh", _noop)
    handler = BUILTIN_COMMAND_REGISTRY["reload"]
    ctx = _callback_ctx(state={})
    out = await handler("", ctx)

    assert "/reload" in out


async def test_reload_reports_skill_diff(monkeypatch):
    import horizon.commands as commands_mod
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    async def _diff():
        return {"loaded": ["new-skill"], "removed": [], "total": 7}

    monkeypatch.setattr(commands_mod, "resync_and_refresh", _diff)
    handler = BUILTIN_COMMAND_REGISTRY["reload"]
    ctx = _callback_ctx(state={})
    out = await handler("", ctx)

    assert "skills: 7" in out
    assert "new-skill" in out


# =============================================================================
# /grant
# =============================================================================


async def test_grant_records_bash_grant_for_command():
    """/grant uses names.BASH, not a hardcoded pre-rename literal (Task 11)."""
    from horizon.commands import BUILTIN_COMMAND_REGISTRY
    from horizon.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY
    from horizon.tools import names

    state: dict = {}
    ctx = _callback_ctx(state=state)
    handler = BUILTIN_COMMAND_REGISTRY["grant"]
    out = await handler("ls /tmp", ctx)

    grants = state.get(POLICY_GRANTS_STATE_KEY) or []
    assert grants[0]["tool_name"] == names.BASH
    assert grants[0]["signature"] == {"command": "ls /tmp"}
    assert "grant" in out.lower() or "ls /tmp" in out


async def test_grant_also_clears_process_spawn_and_write_shapes():
    """A shell command can reach the guard chain as bash(command=...),
    process(action='spawn', command=...), or process(action='write',
    data=...) (Task 4's bash->process split). One /grant must clear all
    three, or the documented recovery path silently no-ops for two of
    them.
    """
    from horizon.commands import BUILTIN_COMMAND_REGISTRY
    from horizon.guardrails.policy_grants import find_matching_grant
    from horizon.tools import names

    state: dict = {}
    ctx = _callback_ctx(state=state)
    handler = BUILTIN_COMMAND_REGISTRY["grant"]
    await handler("git push --force", ctx)

    assert find_matching_grant(
        state, names.BASH, {"command": "git push --force"}
    )
    assert find_matching_grant(
        state,
        names.PROCESS,
        {"action": "spawn", "command": "git push --force"},
    )
    assert find_matching_grant(
        state,
        names.PROCESS,
        {"action": "write", "data": "git push --force"},
    )


async def test_grant_no_args_lists_active_grants():
    from horizon.commands import BUILTIN_COMMAND_REGISTRY
    from horizon.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY

    state: dict = {
        POLICY_GRANTS_STATE_KEY: [
            {"tool_name": "terminal", "signature": {"command": "ls /tmp"}}
        ]
    }
    ctx = _callback_ctx(state=state)
    handler = BUILTIN_COMMAND_REGISTRY["grant"]
    out = await handler("", ctx)

    assert "ls /tmp" in out or "terminal" in out
    assert "1" in out


# =============================================================================
# Slash dispatcher
# =============================================================================


async def test_dispatcher_routes_to_builtin_registry(monkeypatch):
    from horizon.commands.dispatcher import make_slash_command_dispatcher
    from horizon.memory import dream_review

    spy = AsyncMock(return_value={"success": True, "sessions_reviewed": 0})
    monkeypatch.setattr(dream_review, "_run_dream_review_for_user", spy)

    dispatch = make_slash_command_dispatcher()
    ctx = _callback_ctx(state={})
    llm_request = _llm_request_with_text("/dream-review")

    result = await dispatch(callback_context=ctx, llm_request=llm_request)

    assert result is not None
    spy.assert_awaited_once()


async def test_dispatcher_ignores_non_slash_turns():
    from horizon.commands.dispatcher import make_slash_command_dispatcher

    dispatch = make_slash_command_dispatcher()
    ctx = _callback_ctx(state={})
    llm_request = _llm_request_with_text("hello world")

    result = await dispatch(callback_context=ctx, llm_request=llm_request)

    assert result is None


async def test_dispatcher_ignores_unknown_slash_commands():
    from horizon.commands.dispatcher import make_slash_command_dispatcher

    dispatch = make_slash_command_dispatcher()
    ctx = _callback_ctx(state={})
    llm_request = _llm_request_with_text("/does-not-exist")

    result = await dispatch(callback_context=ctx, llm_request=llm_request)

    assert result is None


# =============================================================================
# /sandbox-upgrade
# =============================================================================


async def test_sandbox_upgrade_registered():
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    assert "sandbox-upgrade" in BUILTIN_COMMAND_REGISTRY


async def test_sandbox_upgrade_reports_upgraded(monkeypatch):
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    async def fake_upgrade(user_id):
        assert user_id == "ada"
        return {"status": "upgraded", "version": "v0_3_1"}

    monkeypatch.setattr(
        "horizon.conversation.session_start.upgrade_user_sandbox", fake_upgrade
    )
    out = await BUILTIN_COMMAND_REGISTRY["sandbox-upgrade"](
        "", _callback_ctx(state={}, user_id="ada")
    )
    assert "v0_3_1" in out and "upgrad" in out.lower()


async def test_sandbox_upgrade_reports_already_current(monkeypatch):
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    async def fake_upgrade(user_id):
        return {"status": "already_current", "version": "v0_3_1"}

    monkeypatch.setattr(
        "horizon.conversation.session_start.upgrade_user_sandbox", fake_upgrade
    )
    out = await BUILTIN_COMMAND_REGISTRY["sandbox-upgrade"](
        "", _callback_ctx(state={}, user_id="ada")
    )
    assert "already" in out.lower()


async def test_sandbox_upgrade_reports_unavailable_on_local_backend(
    monkeypatch,
):
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    async def fake_upgrade(user_id):
        return {
            "status": "unavailable",
            "reason": "sandbox backend not enabled",
        }

    monkeypatch.setattr(
        "horizon.conversation.session_start.upgrade_user_sandbox", fake_upgrade
    )
    out = await BUILTIN_COMMAND_REGISTRY["sandbox-upgrade"](
        "", _callback_ctx(state={}, user_id="ada")
    )
    assert "unavailable" in out.lower()


async def test_sandbox_upgrade_handles_missing_user():
    from horizon.commands import BUILTIN_COMMAND_REGISTRY

    ctx = SimpleNamespace(state={}, user_id=None, _invocation_context=None)
    out = await BUILTIN_COMMAND_REGISTRY["sandbox-upgrade"]("", ctx)
    assert "no user" in out.lower()
