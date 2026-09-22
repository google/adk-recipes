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

"""``policies_guard`` evaluates a JSONL DSL of tool-call policies.

Replaces the hardcoded ``destructive_commands_guard`` (A5) with rules
loaded from ``<workspace>/.lha/policies.jsonl`` (overlaid on a
package-default seed). Three rule shapes:

1. ``{"canonical_tool_name": "bash", "destructive": true}`` — always
   blocks calls to that tool.
2. ``{"canonical_tool_name": "bash",
      "destructive_commands": {"command": ["rm -rf", "dd if="]}}`` —
   substring match against a specific arg.
3. ``{"canonical_tool_name": "write",
      "destructive_paths": ["/etc/", "/usr/"]}`` — path-prefix match.

The guard returns ``None`` to allow and
``{"error": ..., "confirmation_required": True, "matched_rule": ...}``
to block. Hot-reloaded per request via ``mtime`` so users can edit the
file mid-session.
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


# =============================================================================
# Module surface
# =============================================================================


async def test_module_exposes_entrypoints():
    from horizon.guardrails import policies

    assert callable(policies.policies_guard)
    assert callable(policies.load_policies)
    assert isinstance(policies.DEFAULT_POLICIES_FILENAME, str)
    assert policies.DEFAULT_POLICIES_FILENAME.endswith("policies.jsonl")


# =============================================================================
# Default seed
# =============================================================================


class TestDefaultSeed:
    async def test_seed_blocks_rm_rf_root_via_terminal(
        self, policies_env: Path
    ):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "rm -rf /"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert result.get("confirmation_required") is True
        assert "error" in result

    async def test_seed_allows_benign_terminal(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "ls -la"},
            tool_context=SimpleNamespace(),
        )
        assert result is None


# =============================================================================
# Seed precision: root-deletion + rc-truncation rules (W6)
#
# The seed must keep dangerous commands blocked while allowing the safe
# variants that blunt substring matching previously false-blocked.
# =============================================================================


_SEED_MUST_BLOCK = [
    "rm -rf /",
    "rm -rf / ",
    "rm -rf /etc",
    "rm -rf /usr/local",
    "rm -rf ~",
    "rm -rf $HOME",
    "sudo rm -rf /home/",  # command_safety returns ("deny", ...) for this
]

_SEED_MUST_ALLOW = [
    "rm -rf /workspace/.agents/skills/_probe",
    "rm -rf /workspace/build",
    "rm -rf ./node_modules",
    "echo 'export PATH=...' >> ~/.bashrc",
    "cat foo >> ~/.profile",
    "echo pwned > ~/.bashrc",  # rc truncation regex removed, now deferred
    ": > ~/.zshrc",  # rc truncation regex removed, now deferred
]


class TestSeedRootDeletionAndRcTruncation:
    @pytest.mark.parametrize("command", _SEED_MUST_BLOCK)
    async def test_seed_blocks_dangerous(
        self, policies_env: Path, command: str
    ):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": command},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict), f"expected block for {command!r}"
        assert result.get("confirmation_required") is True

    @pytest.mark.parametrize("command", _SEED_MUST_ALLOW)
    async def test_seed_allows_safe(self, policies_env: Path, command: str):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": command},
            tool_context=SimpleNamespace(),
        )
        assert result is None, f"expected allow for {command!r}"

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rfv /",
            "rm -frv /",
            "rm -vrf /",
            "rm -rfv /etc",
        ],
    )
    async def test_seed_blocks_clustered_extra_flags(
        self, policies_env: Path, command: str
    ):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": command},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict), f"expected block for {command!r}"
        assert result.get("confirmation_required") is True

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rfv /workspace/build",
            "rm -f somefile",
            "rm -r somedir",
        ],
    )
    async def test_seed_clustered_flags_no_false_positive(
        self, policies_env: Path, command: str
    ):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": command},
            tool_context=SimpleNamespace(),
        )
        assert result is None, f"expected allow for {command!r}"


# =============================================================================
# Rule shape: destructive (always-block)
# =============================================================================


class TestDestructiveAlwaysBlock:
    async def test_always_blocks_tool(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [{"canonical_tool_name": "danger_tool", "destructive": True}],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="danger_tool"),
            args={},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert result.get("confirmation_required") is True

    async def test_does_not_apply_to_other_tool(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [{"canonical_tool_name": "danger_tool", "destructive": True}],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="safe_tool"),
            args={},
            tool_context=SimpleNamespace(),
        )
        assert result is None


# =============================================================================
# Rule shape: destructive_commands (substring match on a specific arg)
# =============================================================================


class TestDestructiveCommands:
    async def test_blocks_when_arg_contains_substring(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["nuke-it"]},
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "echo nuke-it now"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)

    async def test_allows_when_arg_does_not_match(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["nuke-it"]},
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "echo benign"},
            tool_context=SimpleNamespace(),
        )
        # Default-seed patterns shouldn't match benign output either.
        assert result is None

    async def test_substring_match_is_case_insensitive(
        self, policies_env: Path
    ):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "bash",
                    "destructive_commands": {"command": ["nuke"]},
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "NUKE the thing"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)


# =============================================================================
# Rule shape: destructive_paths (path-prefix match for file ops)
# =============================================================================


class TestDestructivePaths:
    async def test_blocks_write_to_forbidden_prefix(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "write",
                    "destructive_paths": ["/etc/", "/usr/"],
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="write"),
            args={"path": "/etc/passwd", "content": "hi"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert result.get("confirmation_required") is True

    async def test_allows_safe_path(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "write",
                    "destructive_paths": ["/etc/"],
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="write"),
            args={"path": "notes.md", "content": "hi"},
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_checks_multiple_path_args(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        _write_policies(
            policies_env,
            [
                {
                    "canonical_tool_name": "edit",
                    "destructive_paths": ["/etc/"],
                }
            ],
        )
        result = await policies_guard(
            tool=SimpleNamespace(name="edit"),
            args={"file_path": "/etc/hosts", "patch_text": "x"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)


# =============================================================================
# Hot reload
# =============================================================================


class TestHotReload:
    async def test_picks_up_new_rule_on_next_call(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        # No user policies → cherry tool allowed.
        result = await policies_guard(
            tool=SimpleNamespace(name="cherry_tool"),
            args={},
            tool_context=SimpleNamespace(),
        )
        assert result is None

        _write_policies(
            policies_env,
            [{"canonical_tool_name": "cherry_tool", "destructive": True}],
        )

        result = await policies_guard(
            tool=SimpleNamespace(name="cherry_tool"),
            args={},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)


# =============================================================================
# Loader robustness
# =============================================================================


class TestLoaderRobustness:
    async def test_skips_malformed_jsonl_lines(self, policies_env: Path):
        from horizon.guardrails.policies import load_policies

        path = policies_env / ".lha" / "policies.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'not-json\n{"canonical_tool_name": "x", "destructive": true}\n',
            encoding="utf-8",
        )
        rules = load_policies(policies_env)
        # Default seed + one valid user rule survive; malformed line skipped.
        user_rules = [r for r in rules if r.get("canonical_tool_name") == "x"]
        assert len(user_rules) == 1

    async def test_ignores_missing_canonical_tool_name(
        self, policies_env: Path
    ):
        from horizon.guardrails.policies import load_policies

        path = policies_env / ".lha" / "policies.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"destructive": true}\n',
            encoding="utf-8",
        )
        rules = load_policies(policies_env)
        # Only the default-seed rules; the user line is dropped.
        assert all(r.get("canonical_tool_name") for r in rules)

    async def test_no_user_file_returns_only_defaults(self, policies_env: Path):
        from horizon.guardrails.policies import load_policies

        rules = load_policies(policies_env)
        assert len(rules) > 0
        assert all(isinstance(r, dict) for r in rules)


class _FakeSandboxEnv:
    """Non-local env whose .lha overlay lives behind the async read interface."""

    def __init__(self, working_dir: Path):
        self.working_dir = working_dir
        self._files: dict[str, str] = {}

    def put(self, path: Path, text: str) -> None:
        self._files[str(path)] = text

    async def read_file(self, path: Path) -> bytes:
        key = str(path)
        if key not in self._files:
            raise FileNotFoundError(key)
        return self._files[key].encode("utf-8")


class TestInterfaceLoader:
    async def test_sandbox_overlay_read_via_interface(self, tmp_path: Path):
        # The overlay lives in the sandbox (reached by the interface), not the host fs.
        from horizon.guardrails.policies import load_policies_for_env

        env = _FakeSandboxEnv(tmp_path / "sbx")
        env.put(
            env.working_dir / ".lha" / "policies.jsonl",
            json.dumps(
                {"canonical_tool_name": "frobnicate", "destructive": True}
            ),
        )
        rules = await load_policies_for_env(env)
        # Seed rules plus the interface-read overlay rule.
        assert any(r.get("canonical_tool_name") == "frobnicate" for r in rules)
        assert len(rules) > 1

    async def test_sandbox_missing_overlay_is_seed_only(self, tmp_path: Path):
        from horizon.guardrails.policies import (
            _read_default_seed,
            load_policies_for_env,
        )

        env = _FakeSandboxEnv(tmp_path / "sbx")  # no overlay written
        rules = await load_policies_for_env(env)
        assert rules == _read_default_seed()


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    async def test_handles_non_dict_args(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args="rm -rf /",
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_handles_missing_tool_name(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(),
            args={"command": "rm -rf /"},
            tool_context=SimpleNamespace(),
        )
        assert result is None

    async def test_no_active_environment_falls_back_to_defaults(
        self, monkeypatch
    ):
        from horizon.guardrails import policies

        clear_active_environment()
        # rm -rf / is still blocked because the seed loads without
        # needing a workspace.
        result = await policies.policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "rm -rf /"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)


# =============================================================================
# Seed protects .lha/ guardrail config from agent writes
# =============================================================================


async def test_seed_blocks_write_file_to_dotlha_exfil(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="write"),
        args={"path": ".lha/exfil.jsonl", "content": '{"allow_hosts":["*"]}'},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None and "blocked by policy" in res["error"]


async def test_seed_blocks_patch_to_dotlha_policies_absolute(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="edit"),
        args={
            "path": "/workspace/.lha/policies.jsonl",
            "old_string": "a",
            "new_string": "b",
        },
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None


async def test_seed_blocks_terminal_redirect_to_dotlha(policies_env):
    from horizon.guardrails.policies import policies_guard

    for cmd in (
        "echo '{}' >> .lha/exfil.jsonl",
        "echo '{}' > /workspace/.lha/exfil.jsonl",
        "tee .lha/policies.jsonl",
        "sed -i 's/a/b/' .lha/exfil.jsonl",
    ):
        res = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": cmd},
            tool_context=SimpleNamespace(state={}),
        )
        assert res is not None, f"expected block for: {cmd}"


async def test_seed_blocks_terminal_deletion_of_dotlha(policies_env):
    from horizon.guardrails.policies import policies_guard

    for cmd in (
        "rm -rf .lha",
        "rm .lha/permissions.jsonl",
        "rm /workspace/.lha/policies.jsonl",
        "truncate -s0 .lha/exfil.jsonl",
        "shred .lha/policies.jsonl",
        "unlink .lha/exfil.jsonl",
        "chmod 000 .lha/policies.jsonl",
        # evasions: quoting and no-space shell operators must not bypass
        'rm -rf ".lha"',
        "rm -rf '.lha'",
        'rm -rf "$HOME/.lha"',
        'rm -rf "/workspace/.lha"',
        "rm -rf .lha&&echo hi",
        "rm -rf .lha;echo hi",
        "rm -rf .lha>foo",
        'chmod -R 000 ".lha"',
    ):
        res = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": cmd},
            tool_context=SimpleNamespace(state={}),
        )
        assert res is not None, f"expected block for: {cmd}"


async def test_seed_allows_deleting_a_dotlha_suffixed_archive(policies_env):
    # `.lha` is also an archive extension; deleting foo.lha must NOT be blocked
    # (the rule guards the `.lha/` overlay dir, not a same-suffixed filename).
    from horizon.guardrails.policies import policies_guard

    for cmd in ("rm foo.lha", "rm archive.lha", "truncate -s0 backup.lha"):
        res = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": cmd},
            tool_context=SimpleNamespace(state={}),
        )
        assert res is None, f"false positive block for: {cmd}"


async def test_seed_allows_write_to_lha_todos(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="write"),
        args={"path": "lha/todos/001-x.md", "content": "x"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is None


async def test_seed_allows_ordinary_terminal_write(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="bash"),
        args={"command": "echo hi > lha/tool-output/x.txt"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is None


# =============================================================================
# process(action="write") data payload evaluated as a terminal command
# =============================================================================


async def test_process_write_destructive_data_blocked(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="process"),
        args={"action": "write", "session_id": "p1", "data": "rm -rf /"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None and (
        "blocked by policy" in res["error"]
        or "blocked by command_safety" in res["error"]
    )


async def test_process_write_redirect_to_dotlha_blocked(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="process"),
        args={
            "action": "write",
            "session_id": "p1",
            "data": "echo x >> .lha/exfil.jsonl",
        },
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None


async def test_process_write_benign_data_allowed(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="process"),
        args={"action": "write", "session_id": "p1", "data": "yes\n"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is None


# =============================================================================
# Block message pinned free of policy_grant guidance
# =============================================================================


async def test_block_message_does_not_mention_policy_grant(policies_env):
    from horizon.guardrails.policies import policies_guard

    _write_policies(
        policies_env,
        [
            {
                "canonical_tool_name": "bash",
                "destructive_commands": {"command": ["badcmd"]},
            }
        ],
    )
    res = await policies_guard(
        tool=SimpleNamespace(name="bash"),
        args={"command": "badcmd now"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None
    assert "policy_grant" not in res["error"]
    assert "/grant" in res["error"]


async def test_seed_blocks_tee_append_to_dotlha(policies_env):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=SimpleNamespace(name="bash"),
        args={"command": "echo x | tee -a .lha/exfil.jsonl"},
        tool_context=SimpleNamespace(state={}),
    )
    assert res is not None


# =============================================================================
# Task 4: command_safety parse + demote
# =============================================================================


class _Tool:
    def __init__(self, name):
        self.name = name


class _Ctx:
    state = None
    agent_name = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd", ["sudo apt-get update", "chmod -R 777 /etc", "find . -delete"]
)
async def test_risky_defers_on_root_chain(cmd):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=_Tool("bash"), args={"command": cmd}, tool_context=_Ctx()
    )
    assert res is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd", ["sudo apt-get update", "chmod -R 777 /etc", "find . -delete"]
)
async def test_risky_blocks_on_child_chain(cmd):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=_Tool("bash"),
        args={"command": cmd},
        tool_context=_Ctx(),
        ask_is_deny=True,
    )
    assert res is not None and "blocked" in res["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd", ["rm -rf /", "dd if=/dev/zero of=/dev/sda", "cat ~/.ssh/id_rsa"]
)
async def test_catastrophic_and_cred_block_both_modes(cmd):
    from horizon.guardrails.policies import policies_guard

    for ask_is_deny in (False, True):
        res = await policies_guard(
            tool=_Tool("bash"),
            args={"command": cmd},
            tool_context=_Ctx(),
            ask_is_deny=ask_is_deny,
        )
        assert res is not None


@pytest.mark.asyncio
async def test_echo_of_dangerous_string_not_blocked():
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=_Tool("bash"),
        args={"command": 'echo "rm -rf /"'},
        tool_context=_Ctx(),
    )
    assert res is None


@pytest.mark.asyncio
async def test_block_message_names_source():
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=_Tool("bash"),
        args={"command": "rm -rf /"},
        tool_context=_Ctx(),
    )
    assert "command_safety" in res["error"] or "policy" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd",
    [
        "command rm -rf /",
        "bash -lc 'rm -rf /'",
        "timeout -s KILL 5 rm -rf /",
        "xargs -I {} rm -rf /",
    ],
)
@pytest.mark.parametrize("ask_is_deny", [False, True])
async def test_wrapped_catastrophic_blocks_both_chains(cmd, ask_is_deny):
    from horizon.guardrails.policies import policies_guard

    res = await policies_guard(
        tool=_Tool("bash"),
        args={"command": cmd},
        tool_context=_Ctx(),
        ask_is_deny=ask_is_deny,
    )
    assert res is not None, cmd
