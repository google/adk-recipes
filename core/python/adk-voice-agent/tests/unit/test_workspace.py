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

"""Workspace model, manifest, and environment selection tests."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest
import yaml

from app.threads.model import EventKind, ThreadState
from app.workers import factory, workspace
from app.workers.environment import LocalEnvironment, SandboxEnvironment
from app.workers.factory import (
    get_workspace_root,
    sandbox_owner,
)
from app.workers.manifest import load_workspaces_manifest, repo_source
from app.workers.providers import registry
from app.workers.workspace import (
    HOME_FOLDERS,
    LandingProblem,
    Workspace,
    finish_workspace,
    open_workspace,
    resolve_project,
)
from tests.fakes import REPO, Lab, git, kinds

PROJECT = "projects/sell-the-car"


@pytest.fixture
def env(tmp_path: Path) -> LocalEnvironment:
    return LocalEnvironment(tmp_path / "home")


@pytest.fixture
def local_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "workspaces.local.yaml"
    monkeypatch.setenv("VOICE_AGENT_WORKSPACES_LOCAL_YAML", str(path))

    def write(data: dict | str) -> Path:
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
        return path

    return write


def _active(*thread_ids: str):
    return lambda thread_id: thread_id in thread_ids


async def _open(env, thread_id, project=PROJECT, active=()) -> Workspace:
    return await open_workspace(
        env, project, thread_id, is_active=_active(thread_id, *active)
    )


async def _finish(env, ws: Workspace, **kwargs) -> workspace.Finish:
    return await finish_workspace(
        env, ws, message=f"thread {ws.thread_id}: work", **kwargs
    )


def _write(path: str, name: str, text: str) -> None:
    Path(path, name).write_text(text)


# ---- manifest & environment selection ----------------------------------------


def test_local_overrides_merge_onto_the_shared_manifest(local_yaml):
    base = load_workspaces_manifest()
    seed = base["seed_repositories"][0]["name"]

    local_yaml(
        {
            "repositories": [
                {"name": "mine", "git_url": "https://example.com/m.git"}
            ],
            "seed_repositories": [{"name": seed, "branch": "trunk"}],
        }
    )
    merged = load_workspaces_manifest()

    assert {r["name"] for r in merged["repositories"]} >= {"mine"}
    merged_seed = next(
        r for r in merged["seed_repositories"] if r["name"] == seed
    )
    assert merged_seed["branch"] == "trunk" and "files" in merged_seed, (
        "fields merge per repo"
    )


def test_unreadable_local_overrides_are_ignored(local_yaml):
    local_yaml("repositories: [unclosed")
    assert load_workspaces_manifest() == load_workspaces_manifest(
        Path("/nonexistent/workspaces.local.yaml")
    )


def test_repo_source_resolves_names_seeds_and_defaults(local_yaml):
    local_yaml(
        {
            "repositories": [
                {
                    "name": "my-repo",
                    "git_url": " https://h/r.git ",
                    "branch": "dev",
                }
            ],
            "seed_repositories": [
                {"name": "seeded", "files": {"README.md": "# hi"}},
                {"name": "bad-files", "files": ["not", "a", "dict"]},
            ],
        }
    )
    assert resolve_project("My Repo", title="t") == "code/my-repo"
    assert resolve_project("", title="t").startswith("scratch/"), (
        "a thread without a project gets a scratch folder"
    )
    with pytest.raises(
        ValueError, match=r"no repository named 'nope'.*code/my-repo"
    ):
        resolve_project("code/nope", title="t")
    assert repo_source("My Repo") == ("https://h/r.git", None, "dev", {})
    assert repo_source("seeded") == ("", None, "main", {"README.md": "# hi"})
    assert repo_source("bad-files")[3] == {}
    assert repo_source("unknown") == ("", None, "main", {})


@pytest.mark.asyncio
async def test_environment_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pinned = LocalEnvironment(tmp_path)
    factory.set_environment(pinned)
    try:
        assert (await factory.resolve_environment()).env is pinned
    finally:
        factory.set_environment(None)

    monkeypatch.setenv("COMPUTER_PROVIDER", "host")
    local = await factory.resolve_environment()
    assert isinstance(local.env, LocalEnvironment) and local.notice is None
    # Each user gets their own directory under the root, named without their id.
    assert Path(
        local.env.workspace_root
    ) == get_workspace_root() / sandbox_owner("local")


@pytest.mark.asyncio
async def test_a_recreated_computer_comes_with_a_notice(
    monkeypatch: pytest.MonkeyPatch,
):
    sandbox = SandboxEnvironment(
        name="sb", url="https://sandbox.test", access_token="at"
    )
    notices: list[str | None] = ["recreated from a newer image", None]

    class FakeProvider:
        name = "fake"

        async def ensure(self, user_id: str) -> SandboxEnvironment:
            return sandbox

        def pop_notice(self, user_id: str) -> str | None:
            return notices.pop(0)

    monkeypatch.setenv("COMPUTER_PROVIDER", "fake")
    monkeypatch.setitem(registry._FACTORIES, "fake", FakeProvider)
    factory.set_environment(None)
    try:
        first = await factory.resolve_environment()
        assert (
            first.env is sandbox
            and first.notice == "recreated from a newer image"
        )
        # Cached for the user afterwards: no second ensure, no repeated notice.
        assert (await factory.resolve_environment()).notice is None
        factory.invalidate_environment()
        assert (await factory.resolve_environment()).notice is None
    finally:
        factory.set_environment(None)


# ---- layout ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_layout_and_projects(env: LocalEnvironment):
    await workspace.ensure_home(env)
    await workspace.ensure_home(env)  # idempotent
    assert all((env.root / name).is_dir() for name in HOME_FOLDERS)

    today = date(2026, 10, 23)
    resolve = workspace.resolve_project
    assert (
        resolve("", title="Hello note", today=today)
        == "scratch/2026-10-23-hello-note"
    )
    assert resolve(REPO, title="x") == f"code/{REPO}"
    assert resolve("Sell the car", title="x") == PROJECT
    assert (
        workspace.thread_dir("/h", "thread-1")
        == "/h/.voice-agent/threads/thread-1"
    )


@pytest.mark.asyncio
async def test_a_new_project_is_a_git_repo_holding_what_was_there(
    env: LocalEnvironment,
):
    root = env.root / PROJECT
    root.mkdir(parents=True)
    (root / "notes.md").write_text("# Car\n")

    assert await workspace.prepare_project(env, PROJECT)
    assert not await workspace.prepare_project(env, PROJECT)
    assert git(str(root), "status", "--porcelain") == ""
    assert git(str(root), "ls-files").split() == ["notes.md"]
    assert git(str(root), "branch", "--show-current").strip() == "main"
    exclude = (root / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/.worktrees/" in exclude


# ---- leases and worktrees ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_writer_works_in_place_under_a_lease(
    env: LocalEnvironment,
):
    ws = await _open(env, "thread-a")
    root = str(env.root / PROJECT)
    assert (ws.path, ws.root, ws.branch) == (root, root, "main")
    assert not ws.isolated and ws.created
    lease = json.loads(
        (
            env.root / ".voice-agent/leases/projects__sell-the-car.json"
        ).read_text()
    )
    assert lease["thread_id"] == "thread-a" and lease["since"]

    again = await _open(env, "thread-a")
    assert again.path == ws.path and not again.created, "opening is idempotent"

    _write(ws.path, "price.md", "# Price\n")
    finish = await _finish(env, ws)
    assert finish.files == ["price.md"] and finish.landed
    assert finish.summary == "1 file(s) changed (+1 -0)"
    assert finish.artifacts[0]["title"] == "Price"
    assert finish.sha == git(root, "rev-parse", "HEAD").strip()
    assert (
        git(root, "log", "-1", "--format=%s").strip() == "thread thread-a: work"
    )
    assert not (
        env.root / ".voice-agent/leases/projects__sell-the-car.json"
    ).exists()


@pytest.mark.asyncio
async def test_losing_the_computer_while_releasing_the_lease_keeps_the_turn(
    env: LocalEnvironment, monkeypatch: pytest.MonkeyPatch
):
    ws = await _open(env, "thread-a")
    _write(ws.path, "price.md", "# Price\n")
    real_read = env.read_file

    async def unreachable(path: str, **kw):
        if "leases" in path:
            raise RuntimeError(
                "Couldn't reach your computer (/files/read: ConnectTimeout)."
            )
        return await real_read(path, **kw)

    monkeypatch.setattr(env, "read_file", unreachable)

    finish = await _finish(env, ws)

    assert finish.files == ["price.md"] and finish.landed
    # The lease stays behind; a finished thread's lease is free to the next one.
    monkeypatch.setattr(env, "read_file", real_read)
    nxt = await _open(env, "thread-b")
    assert not nxt.isolated


@pytest.mark.asyncio
async def test_a_second_writer_gets_a_worktree_that_lands_on_main(
    env: LocalEnvironment,
):
    a = await _open(env, "thread-a")
    b = await _open(env, "thread-b", active=["thread-a"])
    assert b.isolated and b.created and b.branch == "agent/thread-b"
    assert b.path == f"{a.root}/.worktrees/thread-b"
    assert git(a.root, "status", "--porcelain") == "", ".worktrees/ is excluded"

    _write(a.path, "a.md", "# A\n")  # A's edits, not committed yet
    _write(b.path, "b.md", "# B\n")
    landed = await _finish(env, b)
    assert landed.landed and landed.files == ["b.md"]
    assert landed.workdir == a.root
    assert Path(a.root, "b.md").exists() and not Path(b.path).exists()
    assert "agent/thread-b" not in git(
        a.root, "branch", "--format=%(refname:short)"
    )

    finish = await _finish(env, a)
    assert finish.files == ["a.md"], (
        "another thread's landed work isn't this one's"
    )
    assert git(a.root, "log", "--format=%s").splitlines()[:2] == [
        "thread thread-a: work",
        "thread thread-b: work",
    ]


@pytest.mark.asyncio
async def test_a_conflicting_worktree_waits_and_lands_once_resolved(
    env: LocalEnvironment,
):
    a = await _open(env, "thread-a")
    b = await _open(env, "thread-b", active=["thread-a"])
    _write(a.path, "notes.md", "from A\n")
    await _finish(env, a)
    _write(b.path, "notes.md", "from B\n")

    finish = await _finish(env, b)
    assert not finish.landed and finish.problem is LandingProblem.CONFLICT
    assert finish.problem_files == ["notes.md"]
    assert finish.workdir == b.path and Path(b.path).is_dir()
    assert git(b.path, "status", "--porcelain") == "", "the merge was aborted"

    reopened = await _open(env, "thread-b")
    assert reopened.path == b.path and not reopened.created
    # What the harness does when told about the conflict; the server commits.
    subprocess.run(
        ["git", "-C", b.path, "merge", "-q", "main"],
        capture_output=True,
        check=False,
    )
    _write(b.path, "notes.md", "from A and B\n")
    finish = await _finish(env, reopened)
    assert finish.landed and finish.files == ["notes.md"]
    assert Path(a.root, "notes.md").read_text() == "from A and B\n"


@pytest.mark.asyncio
async def test_overlapping_uncommitted_edits_block_landing(
    env: LocalEnvironment,
):
    a = await _open(env, "thread-a")
    b = await _open(env, "thread-b", active=["thread-a"])
    _write(a.path, "notes.md", "A, not committed\n")
    _write(b.path, "notes.md", "from B\n")
    _write(b.path, "other.md", "from B\n")

    finish = await _finish(env, b)
    assert not finish.landed and finish.problem is LandingProblem.BLOCKED
    assert finish.problem_files == ["notes.md"]
    assert Path(a.root, "notes.md").read_text() == "A, not committed\n"


@pytest.mark.asyncio
async def test_a_stale_lease_is_taken_over_when_the_tree_is_clean(
    env: LocalEnvironment,
):
    await _open(env, "thread-a")  # thread-a is gone without finishing
    b = await _open(env, "thread-b")  # thread-a is not active any more
    assert not b.isolated

    _write(b.path, "left-behind.md", "x\n")  # b is gone too, leaving edits
    c = await _open(env, "thread-c")
    assert c.isolated, "a dirty tree is never shared, whoever left it"


# ---- code repos --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_threads_commit_on_their_own_branch(env: LocalEnvironment):
    project = f"code/{REPO}"
    ws = await _open(env, "thread-a", project)
    root = ws.root
    assert ws.path == root and ws.branch == "agent/thread-a" and not ws.isolated
    assert git(root, "branch", "--show-current").strip() == "agent/thread-a"
    base = await workspace.head_sha(env, ws.path)
    main_before = git(root, "rev-parse", "main")

    _write(ws.path, "greet.py", "x = 1\n")
    finish = await _finish(env, ws, base=base)
    assert finish.files == ["greet.py"]
    assert finish.sha == git(root, "rev-parse", "agent/thread-a").strip()
    assert git(root, "branch", "--show-current").strip() == "main"
    assert git(root, "rev-parse", "main") == main_before

    # The next iteration continues on the same branch.
    again = await _open(env, "thread-a", project)
    assert Path(again.path, "greet.py").exists()

    # A concurrent code thread gets a worktree from the default branch; it isn't merged.
    other = await _open(env, "thread-b", project, active=["thread-a"])
    assert other.isolated and not Path(other.path, "greet.py").exists()
    _write(other.path, "other.py", "y = 2\n")
    finish = await _finish(
        env, other, base=await workspace.head_sha(env, other.path)
    )
    assert finish.files == ["other.py"] and finish.landed
    assert Path(other.path).is_dir(), "a code worktree is kept with its branch"


# ---- housekeeping ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_scratch_folders_are_pruned(env: LocalEnvironment):
    old = env.root / "scratch/2026-01-01-old"
    for name in ("2026-01-01-old", "2026-10-01-recent", "2026-01-02-leased"):
        await workspace.prepare_project(env, f"scratch/{name}")
    await open_workspace(
        env, "scratch/2026-01-02-leased", "thread-l", is_active=_active()
    )
    month_ago = time.time() - 40 * 86400
    for folder in (old, env.root / "scratch/2026-01-02-leased"):
        for path in [folder, *folder.rglob("*")]:
            os.utime(path, (month_ago, month_ago), follow_symlinks=False)

    assert await workspace.prune_scratch(env) == ["scratch/2026-01-01-old"]
    assert not old.exists()
    assert (env.root / "scratch/2026-10-01-recent").exists()
    assert (env.root / "scratch/2026-01-02-leased").exists()


# ---- end to end ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_threads_on_one_project(lab: Lab):
    shared = "projects/shared"
    root = Path(lab.env.workspace_root, shared)
    lab.agent.script(
        # A works in place and waits until B has its worktree.
        {
            "steps": [
                {"write": {"a.md": "# From A\n"}},
                {"run": "until [ -d .worktrees ]; do sleep 0.05; done"},
            ]
        },
        {"steps": [{"write": {"b.md": "# From B\n"}}]},
    )
    a = await lab.registry.dispatch("Write A", shared, title="A")
    await lab.wait_for_calls(1)
    b = await lab.registry.dispatch("Write B", shared, title="B")
    a, b = await lab.settle(a.thread_id), await lab.settle(b.thread_id)

    assert a.state is ThreadState.COMPLETED, a.error
    assert b.state is ThreadState.COMPLETED, b.error
    calls = {c["prompt"]: c for c in lab.agent.calls}
    assert calls["Write A"]["cwd"] == str(root.resolve())
    assert calls["Write B"]["cwd"] == str(
        (root / ".worktrees" / b.thread_id).resolve()
    )
    assert (a.files_changed, b.files_changed) == (["a.md"], ["b.md"])
    assert a.workdir == b.workdir == str(root)
    assert (root / "a.md").exists() and (root / "b.md").exists()
    assert not (root / ".worktrees" / b.thread_id).exists()
    log = git(str(root), "log", "--format=%s").splitlines()
    assert (
        f"thread {a.thread_id}: A" in log and f"thread {b.thread_id}: B" in log
    )
    assert not list(
        Path(lab.env.workspace_root, ".voice-agent/threads").glob("*")
    )


@pytest.mark.asyncio
async def test_a_merge_conflict_warns_and_lands_once_resolved(lab: Lab):
    shared = "projects/shared"
    root = Path(lab.env.workspace_root, shared)
    lab.agent.script(
        {
            "steps": [
                {"write": {"notes.md": "from A\n"}},
                {"run": "until [ -d .worktrees ]; do sleep 0.05; done"},
            ]
        },
        # B writes the same file once A's work is on main.
        {
            "steps": [
                {
                    "run": "until git log main --format=%s | grep -q '^thread '; do sleep 0.05; done"
                },
                {"write": {"notes.md": "from B\n"}},
            ]
        },
        # Continuing: the harness merges main and resolves the conflict.
        {
            "steps": [
                {"run": "git merge -q main >/dev/null 2>&1; true"},
                {"write": {"notes.md": "from A and B\n"}},
            ]
        },
    )
    a = await lab.registry.dispatch("Write A", shared, title="A")
    await lab.wait_for_calls(1)
    b = await lab.registry.dispatch("Write B", shared, title="B")
    a = await lab.settle(a.thread_id)
    b = await lab.settle(b.thread_id)
    assert a.state is ThreadState.COMPLETED, a.error
    assert b.state is ThreadState.COMPLETED, b.error
    warning = next(
        e
        for e in b.events
        if e.kind is EventKind.WARNING and e.data.get("warning") == "landing"
    )
    assert warning.data["files"] == ["notes.md"]
    assert (root / ".worktrees" / b.thread_id).exists()

    await lab.registry.steer(
        b.thread_id, "Merge main and resolve the conflict in notes.md"
    )
    b = await lab.settle(b.thread_id)
    assert b.state is ThreadState.COMPLETED, b.error
    assert (root / "notes.md").read_text() == "from A and B\n"
    assert not (root / ".worktrees" / b.thread_id).exists()
    assert kinds(b).count("completed") == 2


# ---- keeping a one-off ----------------------------------------------------------


@pytest.mark.asyncio
async def test_only_an_idle_one_off_moves_and_only_into_projects(
    tmp_path: Path,
):
    from app.workers.environment import LocalEnvironment
    from app.workers.workspace import PromoteError, promote_scratch

    env = LocalEnvironment(tmp_path)
    one_off = tmp_path / "scratch" / "2026-10-27-europe"
    (one_off / ".worktrees" / "thread-1").mkdir(parents=True)
    (one_off / "plan.md").write_text("# Plan")

    with pytest.raises(PromoteError, match="still working"):
        await promote_scratch(
            env, "scratch/2026-10-27-europe", "projects/europe-trip"
        )
    (one_off / ".worktrees" / "thread-1").rmdir()
    (tmp_path / "projects" / "taken").mkdir(parents=True)
    with pytest.raises(PromoteError, match="already exists"):
        await promote_scratch(
            env, "scratch/2026-10-27-europe", "projects/taken"
        )
    with pytest.raises(PromoteError, match="gone"):
        await promote_scratch(env, "scratch/2026-01-01-old", "projects/old")
    with pytest.raises(PromoteError, match="Only a one-off"):
        await promote_scratch(env, "code/payments-api", "projects/payments")

    await promote_scratch(
        env, "scratch/2026-10-27-europe", "projects/europe-trip"
    )
    assert (
        tmp_path / "projects" / "europe-trip" / "plan.md"
    ).read_text() == "# Plan"
    assert not one_off.exists()


@pytest.mark.asyncio
async def test_git_env_disables_hooks_and_config_overrides_when_token_supplied(
    env: LocalEnvironment,
    tmp_path: Path,
):
    project = f"code/{REPO}"
    ws = await open_workspace(
        env,
        project,
        "thread-hook-test",
        is_active=_active("thread-hook-test"),
        github_token="fake-github-token-1",
    )
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    git(ws.root, "remote", "add", "origin", str(remote))
    hooks_dir = Path(ws.root) / ".git" / "hooks"
    marker = tmp_path / "hook-pwned.txt"
    for hook_name in ("post-checkout", "pre-push"):
        hook = hooks_dir / hook_name
        hook.write_text(
            f"#!/bin/sh\nenv > {marker}\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
    git(ws.root, "config", "core.fsmonitor", f"sh -c 'env > {marker}'")

    ws2 = await open_workspace(
        env,
        project,
        "thread-hook-test-2",
        is_active=_active("thread-hook-test", "thread-hook-test-2"),
        github_token="fake-github-token-1",
    )
    _write(ws.path, "feature.py", "x = 1\n")
    await _finish(env, ws, base=await workspace.head_sha(env, ws.path))
    pushed, err = await workspace.push_branch(
        env,
        project,
        ws.branch,
        github_token="fake-github-token-1",
    )
    assert pushed, err
    assert ws2.isolated
    assert not marker.exists()

    env_vars = workspace.git_env("fake-github-token-1")
    assert env_vars["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env_vars["GIT_CONFIG_COUNT"] == "5"
    assert env_vars["GIT_CONFIG_KEY_1"] == "core.hooksPath"
    assert env_vars["GIT_CONFIG_VALUE_1"] == "/dev/null"
    assert env_vars["GIT_CONFIG_KEY_2"] == "core.fsmonitor"
    assert env_vars["GIT_CONFIG_VALUE_2"] == "false"
    assert env_vars["GIT_CONFIG_KEY_3"] == "http.sslVerify"
    assert env_vars["GIT_CONFIG_VALUE_3"] == "true"
    assert env_vars["GIT_CONFIG_KEY_4"] == "http.proxy"
    assert env_vars["GIT_CONFIG_VALUE_4"] == ""


@pytest.mark.asyncio
async def test_worktree_landing_warns_about_gitignored_files(
    env: LocalEnvironment,
    caplog: pytest.LogCaptureFixture,
):
    import logging

    a = await _open(env, "thread-a")
    _write(a.path, ".gitignore", "secret.env\n")
    await _finish(env, a)

    a2 = await _open(env, "thread-a")
    b = await _open(env, "thread-b", active=["thread-a"])
    assert b.isolated
    _write(b.path, "secret.env", "API_KEY=123\n")
    _write(b.path, "tracked.md", "hello\n")

    with caplog.at_level(logging.WARNING):
        finish = await _finish(env, b)
    assert finish.landed
    assert any(
        "secret.env" in r.message and "ignored files" in r.message
        for r in caplog.records
    )
    await _finish(env, a2)
