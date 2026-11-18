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

"""Permission policy, run budgets, and clone file reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.mcp_permissions import matching_rule
from app.workers.environment import LocalEnvironment
from app.workers.policy import load_policy, read_clone_file


def test_default_policy_loads_permissions_and_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(tmp_path))
    policy = load_policy()
    assert "*metadata.google.internal*" in policy.permissions["deny"]
    assert "gmail.send*" in policy.mcp_gates
    assert policy.stall_timeout_s == 5 * 60


def test_user_policy_overrides_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(tmp_path))
    (tmp_path / "policy.yaml").write_text(
        'permissions:\n  deny: ["npm publish*"]\n'
        'mcp_gates:\n  ask: ["google_docs.*"]\n'
        "run_budget:\n  stall_timeout_min: 10\n"
    )
    policy = load_policy()
    # User lists extend the defaults rather than replacing them.
    assert "npm publish*" in policy.permissions["deny"]
    assert "*metadata.google.internal*" in policy.permissions["deny"]
    assert (
        "google_docs.*" in policy.mcp_gates
        and "gmail.send*" in policy.mcp_gates
    )
    assert policy.stall_timeout_s == 600


def test_github_write_tools_are_gated_and_reads_run_freely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICE_AGENT_STATE_DIR", str(tmp_path))
    gates = load_policy().mcp_gates

    write_tools = [
        "github.merge_pull_request",
        "github.create_pull_request",
        "github.update_pull_request",
        "github.update_pull_request_branch",
        "github.pull_request_review_write",
        "github.create_pull_request_review",
        "github.submit_pending_pull_request_review",
        "github.push_files",
        "github.create_or_update_file",
        "github.create_branch",
        "github.add_issue_comment",
        "github.add_comment_to_pending_review",
        "github.create_issue",
        "github.update_issue",
        "github.create_repository",
        "github.fork_repository",
        "github.delete_file",
    ]
    for tool in write_tools:
        assert matching_rule(tool, gates) is not None, f"{tool} should be gated"

    read_tools = [
        "github.get_file_contents",
        "github.list_pull_requests",
        "github.search_code",
        "github.list_issues",
        "github.get_pull_request",
    ]
    for tool in read_tools:
        assert matching_rule(tool, gates) is None, f"{tool} should not be gated"


@pytest.mark.asyncio
async def test_read_clone_file_enforces_clone_and_hidden_boundaries(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "report.md").write_text("# Report\n", encoding="utf-8")
    (clone / ".voice-agent").mkdir()
    (clone / ".voice-agent" / "mcp.json").write_text("{}", encoding="utf-8")

    env = LocalEnvironment(tmp_path)
    content = await read_clone_file(
        env, str(clone), "report.md", max_bytes=1024, allow_hidden=False
    )
    assert content == b"# Report\n"

    with pytest.raises(ValueError, match="not a path inside"):
        await read_clone_file(env, str(clone), "../secret.txt", max_bytes=1024)

    with pytest.raises(ValueError, match="hidden directory"):
        await read_clone_file(
            env,
            str(clone),
            ".voice-agent/mcp.json",
            max_bytes=1024,
            allow_hidden=False,
        )
