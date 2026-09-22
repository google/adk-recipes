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

"""Relaxed default posture (Y): shell + a few benign non-shell tools allow at the
rule layer; everything else stays ask_user. Scary-op gating is layered on top in
_shell_decision/command_safety (see test_permission_guard.py + test_command_safety.py)."""

from __future__ import annotations

from horizon.guardrails.permission_rules import (
    DEFAULT_RULES,
    parse_rule,
    resolve_decision,
)


def _decide(tool_name, command=None, args=None):
    return resolve_decision(
        list(DEFAULT_RULES),
        tool_name=tool_name,
        args=args
        if args is not None
        else ({"command": command} if command else {}),
        command=command,
    )[0]


def test_sandbox_writes_allowed():
    assert _decide("write", args={"path": "x"}) == "allow"
    assert _decide("edit", args={"path": "x"}) == "allow"


def test_shell_allowed_at_rule_layer():
    # bash/process resolve to allow; scary gating is in _shell_decision.
    for cmd in (
        "ls -la",
        "git status -s",
        "git commit -m x",
        "git push origin main",
        "npm install",
        "rm old.txt",
        "mv a b",
        "bq rm -t x.y",
        "chmod -R 755 ./build",
    ):
        assert _decide("bash", command=cmd) == "allow", cmd


def test_process_all_actions_allowed():
    for action in ("list", "poll", "log", "wait", "write", "kill"):
        assert _decide("process", args={"action": action}) == "allow"


def test_opened_benign_tools_allowed():
    # load_skill (covers load and action='reload') is not in this list —
    # it's in permission_guard.READ_ONLY_TOOLS and bypasses resolve_decision
    # entirely, never reaching the default-rule layer this test exercises.
    for tool in ("memory",):
        assert _decide(tool, args={}) == "allow"


def test_other_nonshell_tools_still_ask():
    for tool in ("routine", "send_email"):
        assert _decide(tool, args={}) == "ask_user"


def test_overlay_deny_overrides_default_allow():
    rules = [
        *DEFAULT_RULES,
        parse_rule(
            {"toolName": "bash", "commandPrefix": "rm", "decision": "deny"}
        ),
    ]
    decision, _ = resolve_decision(
        rules, tool_name="bash", args={"command": "rm x"}, command="rm x"
    )
    assert decision == "deny"
