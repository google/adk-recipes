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

"""Unit tests for permission rule parsing, matching, and decision resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from horizon.guardrails.permission_rules import (
    DEFAULT_RULES,
    PERMISSION_GRANTS_STATE_KEY,
    PERMISSION_OVERLAY_FILENAME,
    append_persisted_rule,
    load_persisted_rules,
    parse_rule,
    read_session_grants,
    resolve_decision,
    write_session_grants,
)


def _default_decision(command: str) -> str:
    decision, _ = resolve_decision(
        list(DEFAULT_RULES),
        tool_name="bash",
        args={"command": command},
        command=command,
    )
    return decision


def test_parse_normalizes_gemini_fields():
    rule = parse_rule(
        {
            "toolName": ["a", "b"],
            "commandPrefix": "git status",
            "decision": "allow",
        }
    )
    assert rule is not None
    assert rule.tool_names == ("a", "b")
    assert rule.command_prefixes == ("git status",)
    assert rule.decision == "allow"


def test_parse_rejects_bad_decision():
    assert parse_rule({"toolName": "a", "decision": "maybe"}) is None


def test_tool_wildcard_and_name_match():
    rules = [parse_rule({"toolName": "*", "decision": "ask_user"})]
    decision, _ = resolve_decision(
        rules, tool_name="write", args={}, command=None
    )
    assert decision == "ask_user"


def test_command_prefix_is_token_boundary_aware():
    rules = [
        parse_rule(
            {"toolName": "bash", "commandPrefix": "ls", "decision": "allow"}
        )
    ]
    allow, _ = resolve_decision(
        rules,
        tool_name="bash",
        args={"command": "ls -la"},
        command="ls -la",
    )
    none_match, _ = resolve_decision(
        rules, tool_name="bash", args={"command": "lshw"}, command="lshw"
    )
    assert allow == "allow"
    assert none_match == "ask_user"  # no rule matched → default ask_user


@pytest.mark.parametrize(
    "command,expected",
    [
        # Flag-tolerant: one approval covers any flag ordering.
        ("bq --project_id=X query 'SELECT 1'", "allow"),
        ("bq query 'SELECT 1'", "allow"),
        ("bq --a=1 --b=2 query t", "allow"),
        # ...but a separate-value flag is ambiguous, so it fails safe to a prompt
        # rather than guess that `X` is not the subcommand.
        ("bq --project_id X query t", "ask_user"),
        # A flag value must never impersonate the granted subcommand.
        ("bq -n query rm t", "ask_user"),
        # Privilege escalation is never unwrapped into a plain grant.
        ("sudo bq query t", "ask_user"),
        ("bq rm -t x.y", "ask_user"),  # different subcommand
    ],
)
def test_flagless_prefix_matches_flag_tolerantly(command, expected):
    rules = [
        parse_rule(
            {
                "toolName": "bash",
                "commandPrefix": "bq query",
                "decision": "allow",
            }
        )
    ]
    decision, _ = resolve_decision(
        rules,
        tool_name="bash",
        args={"command": command},
        command=command,
    )
    assert decision == expected


def test_args_pattern_match():
    rules = [
        parse_rule(
            {
                "toolName": "process",
                "argsPattern": r'"action":\s*"list"',
                "decision": "allow",
            }
        )
    ]
    decision, _ = resolve_decision(
        rules, tool_name="process", args={"action": "list"}, command=None
    )
    assert decision == "allow"


def test_last_match_wins():
    rules = [
        parse_rule({"toolName": "bash", "decision": "ask_user"}),
        parse_rule(
            {
                "toolName": "bash",
                "commandPrefix": "git",
                "decision": "allow",
            }
        ),
    ]
    decision, _ = resolve_decision(
        rules,
        tool_name="bash",
        args={"command": "git log"},
        command="git log",
    )
    assert decision == "allow"


def test_deny_wins_over_later_allow():
    rules = [
        parse_rule(
            {
                "toolName": "bash",
                "commandPrefix": "rm",
                "decision": "deny",
                "denyMessage": "no",
            }
        ),
        parse_rule({"toolName": "bash", "decision": "allow"}),
    ]
    decision, rule = resolve_decision(
        rules, tool_name="bash", args={"command": "rm x"}, command="rm x"
    )
    assert decision == "deny"
    assert rule.deny_message == "no"


def test_subagent_constraint(monkeypatch):
    rules = [
        parse_rule(
            {"toolName": "bash", "subagent": "worker", "decision": "allow"}
        )
    ]
    # Matches only when the caller agent is "worker".
    match, _ = resolve_decision(
        rules,
        tool_name="bash",
        args={"command": "x"},
        command="x",
        agent_name="worker",
    )
    no_match, _ = resolve_decision(
        rules,
        tool_name="bash",
        args={"command": "x"},
        command="x",
        agent_name="root",
    )
    assert match == "allow"
    assert no_match == "ask_user"


def test_default_posture_shell_allow_nonshell_ask():
    # terminal blanket-allow at the rule layer; non-opened non-shell tools ask.
    for cmd in (
        "bq ls d",
        "bq rm -t d.t",
        "git push origin main",
        "git commit -m x",
        "gcloud config list",
    ):
        assert _default_decision(cmd) == "allow", cmd
    for tool in ("routine", "send_email"):
        decision, _ = resolve_decision(
            list(DEFAULT_RULES), tool_name=tool, args={}, command=None
        )
        assert decision == "ask_user", tool


def test_default_rules_open_shell_and_benign_tools():
    # load_skill (covers load and action='reload') is not among the default
    # rules — it's in permission_guard.READ_ONLY_TOOLS and bypasses rule
    # resolution entirely.
    names = {tn for r in DEFAULT_RULES for tn in r.tool_names}
    assert {
        "*",
        "bash",
        "process",
        "memory",
    } <= names
    star = [r for r in DEFAULT_RULES if r.tool_names == ("*",)]
    assert star and star[0].decision == "ask_user"


@pytest.mark.asyncio
async def test_append_persisted_rule_writes_jsonl(tmp_path: Path):
    from horizon.environment import LocalEnvironment

    env = LocalEnvironment(working_dir=tmp_path)
    await append_persisted_rule(
        env,
        {"toolName": "bash", "commandPrefix": "bq rm", "decision": "allow"},
    )
    text = (tmp_path / PERMISSION_OVERLAY_FILENAME).read_text()
    assert '"commandPrefix": "bq rm"' in text
    assert text.endswith("\n")


class _FakeSandboxEnv:
    """A non-local env whose .lha overlay lives behind the async read/write interface
    (an in-memory stand-in for the real sandbox file service). Every real sandbox
    shares the same ``/workspace`` working dir, so ``sandbox_name`` is the only
    tenant-distinguishing handle the cache may key on."""

    def __init__(self, working_dir: Path, sandbox_name: str = "lha-test-1"):
        self.working_dir = working_dir
        self.sandbox_name = sandbox_name
        self._files: dict[str, str] = {}

    def cache_identity(self) -> str:
        return self.sandbox_name

    async def read_file(self, path: Path) -> bytes:
        key = str(path)
        if key not in self._files:
            raise FileNotFoundError(key)
        return self._files[key].encode("utf-8")

    async def write_file(self, path: Path, content: str | bytes) -> None:
        self._files[str(path)] = (
            content if isinstance(content, str) else content.decode("utf-8")
        )


@pytest.mark.asyncio
async def test_interface_append_then_load_roundtrip(tmp_path: Path):
    env = _FakeSandboxEnv(tmp_path / "sbx")
    # Empty before anything is saved (read_file raises FileNotFoundError).
    assert await load_persisted_rules(env) == []
    await append_persisted_rule(
        env, {"toolName": "routine", "decision": "allow"}
    )
    rules = await load_persisted_rules(env)
    assert len(rules) == 1
    assert rules[0].tool_names == ("routine",)
    assert rules[0].decision == "allow"
    # The rule was written through the interface, not the host fs.
    assert str(env.working_dir / PERMISSION_OVERLAY_FILENAME) in env._files


@pytest.mark.asyncio
async def test_interface_cache_does_not_leak_across_sandboxes(tmp_path: Path):
    # Every sandbox shares working_dir=/workspace; the cache must NOT key on it,
    # or one tenant's overlay would be served to another on a shared instance.
    shared = Path("/workspace")
    alice = _FakeSandboxEnv(shared, sandbox_name="lha-alice-1")
    bob = _FakeSandboxEnv(shared, sandbox_name="lha-bob-1")
    await append_persisted_rule(
        alice, {"toolName": "routine", "decision": "allow"}
    )

    alice_rules = await load_persisted_rules(alice)
    bob_rules = await load_persisted_rules(bob)
    assert [r.tool_names for r in alice_rules] == [("routine",)]
    assert bob_rules == []  # bob never saw alice's grant


class _FakeState:
    """ADK State stand-in: supports .get / __setitem__ but is NOT a dict subclass."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value


def test_session_grants_roundtrip_on_non_dict_state():
    state = _FakeState()
    assert read_session_grants(state) == []
    rule = {"toolName": "routine", "decision": "allow"}
    assert write_session_grants(state, [rule]) is True
    assert read_session_grants(state) == [rule]
    # Stored under the documented key (so /permissions and the guard agree).
    assert state[PERMISSION_GRANTS_STATE_KEY] == [rule]


def test_session_grants_tolerate_none_state():
    assert read_session_grants(None) == []
    assert (
        write_session_grants(None, [{"toolName": "x", "decision": "allow"}])
        is False
    )


def test_parse_rule_trusted_skips_regex_safety():
    # Trusted defaults are allowed to use complex regexes (the bq/gws rules need lookahead).
    complex_regex = r"(a+)+"
    trusted_rule = parse_rule(
        {
            "toolName": "bash",
            "commandRegex": complex_regex,
            "decision": "allow",
        },
        trusted=True,
    )
    assert trusted_rule is not None
    assert trusted_rule.command_regex == complex_regex

    # The same call without trusted=True (default) should reject the unsafe regex.
    untrusted_rule = parse_rule(
        {
            "toolName": "bash",
            "commandRegex": complex_regex,
            "decision": "allow",
        }
    )
    assert untrusted_rule is None


@pytest.mark.asyncio
async def test_blanket_allow_for_terminal_is_dropped_from_overlay(monkeypatch):
    # An overlay rule that allows ALL terminal commands (no narrowing) must be dropped.
    async def fake_persisted(env):
        return [parse_rule({"toolName": "bash", "decision": "allow"})]

    monkeypatch.setattr(
        "horizon.guardrails.permission_rules.load_persisted_rules",
        fake_persisted,
    )
    from horizon.guardrails.permission_rules import effective_rules

    rules = await effective_rules(env=object(), session_grants=None)
    assert not any(
        r.decision == "allow"
        and "bash" in r.tool_names
        and not r.command_prefixes
        and not r.command_regex
        and not r.args_pattern
        for r in rules
        if r.source in {"overlay", "grant"}
    )


@pytest.mark.asyncio
async def test_narrowed_terminal_allow_survives(monkeypatch):
    async def fake_persisted(env):
        return [
            parse_rule(
                {
                    "toolName": "bash",
                    "commandPrefix": "npm test",
                    "decision": "allow",
                }
            )
        ]

    monkeypatch.setattr(
        "horizon.guardrails.permission_rules.load_persisted_rules",
        fake_persisted,
    )
    from horizon.guardrails.permission_rules import effective_rules

    rules = await effective_rules(env=object(), session_grants=None)
    assert any(r.command_prefixes == ("npm test",) for r in rules)
