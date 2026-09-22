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

"""The tool-name alias layer must not open a security hole.

A rule persisted under a legacy tool name (an overlay file, a session
grant) has to keep resolving to the live tool after a rename, without
letting the aliasing step itself defeat the untrusted-blanket-allow
narrowing check that guards shell tools.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from horizon.guardrails import permission_rules as pr
from horizon.guardrails.permission_guard import resolve_permission_decision
from horizon.guardrails.permission_rules import parse_rule
from horizon.guardrails.policies import policies_guard
from horizon.tools import names

pytestmark = pytest.mark.asyncio


def test_legacy_rule_still_matches_current_tool():
    rule = parse_rule(
        {
            "toolName": "terminal",
            "commandPrefix": ["bq rm"],
            "decision": "allow",
        },
        trusted=False,
    )
    assert rule is not None
    assert names.BASH in rule.tool_names


def test_aliased_blanket_allow_is_still_dropped():
    # Catches: aliasing "terminal" to a live name without also updating
    # _TOOLS_REQUIRING_NARROWING would let an untrusted blanket allow for
    # shell survive _stamp unnarrowed.
    rule = parse_rule({"toolName": "terminal", "decision": "allow"})
    assert rule is not None
    assert names.BASH in rule.tool_names
    assert pr._is_blanket_allow(rule)
    stamped = pr._stamp([rule], "overlay")
    assert stamped == []


async def test_hard_deny_floor_survives_aliasing():
    # dd if=... matches the JSONL destructive_commands substring list
    # directly (policies.py's _evaluate), independent of command_safety.
    blocked = await policies_guard(
        tool=SimpleNamespace(name=names.BASH),
        args={"command": "dd if=/dev/zero of=/dev/sda"},
        tool_context=SimpleNamespace(),
    )
    assert blocked is not None and "error" in blocked


async def test_command_safety_deny_still_fires_for_shell():
    # Catches: if policies._command_for stopped recognizing names.BASH,
    # this would return None (allow) instead of blocking a recursive
    # force-delete of a system root.
    blocked = await policies_guard(
        tool=SimpleNamespace(name=names.BASH),
        args={"command": "rm -rf /"},
        tool_context=SimpleNamespace(),
    )
    assert blocked is not None and "error" in blocked


async def test_command_substitution_still_demotes_to_ask():
    # Catches: aliasing breaking the anti-obfuscation net that demotes
    # command substitution to a prompt regardless of any grant.
    decision, _deny, _prefix, _cmd = await resolve_permission_decision(
        None,
        tool_name=names.BASH,
        args={"command": "echo $(cat /etc/passwd)"},
        state={},
        agent_name="root_agent",
    )
    assert decision == "ask_user"
