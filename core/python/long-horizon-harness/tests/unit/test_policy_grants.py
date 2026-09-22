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

"""Per-session policy grants — HITL bypass for the policies guard.

When ``policies_guard`` blocks a tool call, the model can (after explicit
user approval) call ``policy_grant(action='grant', tool_name=...,
signature=...)`` to record a per-session grant that exempts subsequent
calls with the same ``(tool_name, signature)`` from the block.

A grant matches an in-flight tool call iff:
  * the grant's ``tool_name`` equals the call's tool name, AND
  * every key in the grant's ``signature`` is present in the call's
    ``args`` with the exact same value.

Grants live in ``session.state["_policy_grants"]`` so they travel with
the session (durable, GC'd when the session ends) and cannot bleed
across sessions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from horizon.environment import LocalEnvironment
from horizon.environment_context import (
    clear_active_environment,
    set_active_environment,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def policies_env(tmp_path: Path) -> Iterator[Path]:
    working_dir = tmp_path / "ws"
    working_dir.mkdir()
    set_active_environment(LocalEnvironment(working_dir=working_dir))
    try:
        yield working_dir
    finally:
        clear_active_environment()


def _write_policies(working_dir: Path, rules: list[dict]) -> Path:
    path = working_dir / ".lha" / "policies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rules), encoding="utf-8")
    return path


def _tool_ctx(state: dict) -> SimpleNamespace:
    """Minimal duck-typed ``tool_context`` exposing only ``.state``."""
    return SimpleNamespace(state=state)


# =============================================================================
# Module surface
# =============================================================================


async def test_module_exposes_policy_grant_entrypoint():
    from horizon.guardrails import policy_grants

    assert callable(policy_grants.policy_grant)
    assert isinstance(policy_grants.POLICY_GRANTS_STATE_KEY, str)


# =============================================================================
# Grant recording
# =============================================================================


class TestGrantRecording:
    async def test_grant_appends_to_state(self):
        from horizon.guardrails.policy_grants import (
            POLICY_GRANTS_STATE_KEY,
            policy_grant,
        )

        state: dict = {}
        result = policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is True
        grants = state[POLICY_GRANTS_STATE_KEY]
        assert isinstance(grants, list)
        assert len(grants) == 1
        assert grants[0]["tool_name"] == "bash"
        assert grants[0]["signature"] == {"command": "rm -rf build/"}

    async def test_grant_with_empty_signature_is_rejected(self):
        from horizon.guardrails.policy_grants import (
            POLICY_GRANTS_STATE_KEY,
            policy_grant,
        )

        state: dict = {}
        result = policy_grant(
            action="grant",
            tool_name="bash",
            signature={},
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is False
        assert "error" in result
        assert state.get(POLICY_GRANTS_STATE_KEY) in (None, [])

    async def test_grant_with_non_string_values_is_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(
            action="grant",
            tool_name="bash",
            signature={"timeout": 30},  # type: ignore[dict-item]
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is False

    async def test_grant_without_tool_name_is_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(
            action="grant",
            tool_name="",
            signature={"command": "x"},
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is False

    async def test_duplicate_grant_is_idempotent(self):
        from horizon.guardrails.policy_grants import (
            POLICY_GRANTS_STATE_KEY,
            policy_grant,
        )

        state: dict = {}
        sig = {"command": "rm -rf build/"}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature=sig,
            tool_context=_tool_ctx(state),
        )
        policy_grant(
            action="grant",
            tool_name="bash",
            signature=sig,
            tool_context=_tool_ctx(state),
        )
        assert len(state[POLICY_GRANTS_STATE_KEY]) == 1


# =============================================================================
# Grant matching inside policies_guard
# =============================================================================


class TestGrantBypassesBlock:
    async def test_matching_grant_allows_blocked_call(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard
        from horizon.guardrails.policy_grants import policy_grant

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["rm -rf"]},
                }
            ],
        )
        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        assert result is None

    async def test_different_arg_value_still_blocked(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard
        from horizon.guardrails.policy_grants import policy_grant

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["rm -rf"]},
                }
            ],
        )
        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "rm -rf /"},
            tool_context=_tool_ctx(state),
        )
        assert isinstance(result, dict)
        assert result.get("confirmation_required") is True

    async def test_grant_does_not_match_other_tool(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard
        from horizon.guardrails.policy_grants import policy_grant

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "write",
                    "destructive_paths": ["/etc/"],
                }
            ],
        )
        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="write"),
            args={"path": "/etc/passwd", "content": "x"},
            tool_context=_tool_ctx(state),
        )
        assert isinstance(result, dict)

    async def test_signature_subset_match(self, policies_env: Path):
        """Extra args in the call don't break a grant whose signature is
        a subset of those args."""
        from horizon.guardrails.policies import policies_guard
        from horizon.guardrails.policy_grants import policy_grant

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["nuke"]},
                }
            ],
        )
        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "nuke the cache"},
            tool_context=_tool_ctx(state),
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "nuke the cache", "cwd": "/tmp"},
            tool_context=_tool_ctx(state),
        )
        assert result is None

    async def test_grant_does_not_bleed_across_sessions(
        self, policies_env: Path
    ):
        from horizon.guardrails.policies import policies_guard
        from horizon.guardrails.policy_grants import policy_grant

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["nuke"]},
                }
            ],
        )
        state_a: dict = {}
        state_b: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "nuke me"},
            tool_context=_tool_ctx(state_a),
        )
        # Session B has no grant; same call still blocked.
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "nuke me"},
            tool_context=_tool_ctx(state_b),
        )
        assert isinstance(result, dict)


# =============================================================================
# Grant listing / revocation
# =============================================================================


class TestGrantListAndRevoke:
    async def test_list_returns_active_grants(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "rm -rf build/"},
            tool_context=_tool_ctx(state),
        )
        policy_grant(
            action="grant",
            tool_name="write",
            signature={"path": "/etc/myapp.conf"},
            tool_context=_tool_ctx(state),
        )
        result = policy_grant(action="list", tool_context=_tool_ctx(state))
        assert result["count"] == 2
        names = sorted(g["tool_name"] for g in result["grants"])
        assert names == ["bash", "write"]

    async def test_list_when_no_grants(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(action="list", tool_context=_tool_ctx(state))
        assert result["count"] == 0
        assert result["grants"] == []

    async def test_revoke_by_index_removes_grant(self):
        from horizon.guardrails.policy_grants import (
            POLICY_GRANTS_STATE_KEY,
            policy_grant,
        )

        state: dict = {}
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "a"},
            tool_context=_tool_ctx(state),
        )
        policy_grant(
            action="grant",
            tool_name="bash",
            signature={"command": "b"},
            tool_context=_tool_ctx(state),
        )
        result = policy_grant(
            action="revoke", index=0, tool_context=_tool_ctx(state)
        )
        assert result["revoked"] is True
        remaining = state[POLICY_GRANTS_STATE_KEY]
        assert len(remaining) == 1
        assert remaining[0]["signature"] == {"command": "b"}

    async def test_revoke_out_of_range_is_safe(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(
            action="revoke", index=5, tool_context=_tool_ctx(state)
        )
        assert result["revoked"] is False


# =============================================================================
# Dispatch error branches
# =============================================================================


class TestDispatchErrors:
    async def test_grant_missing_tool_name_kwarg_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(
            action="grant",
            signature={"command": "x"},
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is False
        assert "tool_name" in result["error"]

    async def test_grant_missing_signature_kwarg_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(
            action="grant",
            tool_name="bash",
            tool_context=_tool_ctx(state),
        )
        assert result["granted"] is False
        assert "signature" in result["error"]

    async def test_revoke_missing_index_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(action="revoke", tool_context=_tool_ctx(state))
        assert result["revoked"] is False
        assert "index" in result["error"].lower()

    async def test_unknown_action_rejected(self):
        from horizon.guardrails.policy_grants import policy_grant

        state: dict = {}
        result = policy_grant(action="banana", tool_context=_tool_ctx(state))  # type: ignore[arg-type]
        assert "error" in result
        assert "action" in result["error"].lower()
