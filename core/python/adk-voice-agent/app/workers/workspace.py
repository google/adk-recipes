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

"""Workspace folders (`code/<repo>`, `projects/<slug>`, `scratch/<date>-<slug>`), leases, worktrees, and git commits."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from app.workers.environment import Environment, ExecResult
from app.workers.manifest import known_repos, repo_source

logger = logging.getLogger(__name__)

HOME_FOLDERS = ("inbox", "projects", "code", "scratch", ".voice-agent")
THREADS_DIR = ".voice-agent/threads"
LEASES_DIR = ".voice-agent/leases"
WORKTREES_DIR = ".worktrees"
MAIN = "main"
_EXCLUDED = (f"/{WORKTREES_DIR}/",)

_SLUG_MAX = 48
_NOT_SLUG = re.compile(r"[^a-z0-9]+")

_ARTIFACT_EXTENSIONS = {".md", ".txt", ".csv", ".html"}
_IGNORED_ARTIFACTS = {"agents.md", "claude.md", "gemini.md", "readme.md"}
_MAX_RAW_DIFF_BYTES = 200_000
GIT_IDENTITY_NAME = "ADK Voice Agent"
GIT_IDENTITY_EMAIL = "voice-agent@users.noreply.github.com"

q = shlex.quote


class ProjectKind(StrEnum):
    CODE = "code"
    PROJECT = "projects"
    SCRATCH = "scratch"


def slugify(text: str) -> str:
    slug = _NOT_SLUG.sub("-", text.lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-")


def project_kind(project: str) -> ProjectKind:
    return ProjectKind(project.split("/", 1)[0])


def is_code(project: str) -> bool:
    return project_kind(project) is ProjectKind.CODE


def repo_of(project: str) -> str:
    if not is_code(project):
        raise ValueError(f"{project} is not a code project")
    return project.split("/", 1)[1]


def thread_branch(thread_id: str) -> str:
    return f"agent/{thread_id}"


def thread_dir(home: str, thread_id: str) -> str:
    return f"{home}/{THREADS_DIR}/{thread_id}"


def project_path(home: str, project: str) -> str:
    return f"{home}/{project}"


def resolve_project(
    project: str, *, title: str, today: date | None = None
) -> str:
    """Resolve `project` to `code/<repo>`, `projects/<slug>`, or `scratch/<date>-<slug>`."""
    raw = project.strip().removeprefix("~/").strip("/")
    head, _, rest = raw.partition("/")
    head = head.lower()
    if not raw or (not rest and head in ("scratch", "workspace")):
        day = (today or date.today()).isoformat()
        return f"scratch/{day}-{slugify(title) or 'thread'}"
    repos = [r["name"] for r in known_repos()]
    if head == ProjectKind.CODE:
        name = slugify(rest)
        if name in repos:
            return f"code/{name}"
        options = ", ".join(f"code/{r}" for r in repos) or "none are configured"
        raise ValueError(
            f"There is no repository named {rest!r}. The options are: {options}."
        )
    if head in (ProjectKind.PROJECT, ProjectKind.SCRATCH):
        if not slugify(rest):
            raise ValueError(f"{raw!r} doesn't name a folder.")
        return f"{head}/{slugify(rest)}"
    if rest:
        raise ValueError(
            f"{raw!r} isn't a project. Use code/<repo>, projects/<name>, "
            "scratch/<name>, or leave it empty for a new scratch folder."
        )
    name = slugify(raw)
    return f"code/{name}" if name in repos else f"projects/{name}"


def git_env(token: str = "") -> dict[str, str]:
    """Per-process git identity, GitHub auth header, and hook/proxy lockdown."""
    values = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for role in ("AUTHOR", "COMMITTER"):
        values[f"GIT_{role}_NAME"] = (
            os.getenv(f"GIT_{role}_NAME") or GIT_IDENTITY_NAME
        )
        values[f"GIT_{role}_EMAIL"] = (
            os.getenv(f"GIT_{role}_EMAIL") or GIT_IDENTITY_EMAIL
        )
    entries: list[tuple[str, str]] = []
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode(
            "ascii"
        )
        entries.append(
            (
                "http.https://github.com/.extraheader",
                f"Authorization: Basic {basic}",
            )
        )
    entries.extend(
        [
            ("core.hooksPath", "/dev/null"),
            ("core.fsmonitor", "false"),
            ("http.sslVerify", "true"),
            ("http.proxy", ""),
        ]
    )
    values["GIT_CONFIG_COUNT"] = str(len(entries))
    for idx, (key, val) in enumerate(entries):
        values[f"GIT_CONFIG_KEY_{idx}"] = key
        values[f"GIT_CONFIG_VALUE_{idx}"] = val
    return values


async def _git(
    env: Environment,
    path: str,
    script: str,
    *,
    token: str = "",
    timeout_s: float = 120.0,
) -> ExecResult:
    return await env.exec(
        f"cd {q(path)} || exit 2\n{script}",
        env=git_env(token),
        timeout_s=timeout_s,
    )


async def _git_ok(
    env: Environment, path: str, script: str, doing: str, **kwargs
) -> str:
    result = await _git(env, path, script, **kwargs)
    if not result.ok:
        raise RuntimeError(f"Could not {doing}: {result.describe_failure()}")
    return result.stdout


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


async def head_sha(env: Environment, path: str) -> str | None:
    result = await _git(env, path, "git rev-parse HEAD", timeout_s=30.0)
    return (result.stdout.strip() or None) if result.ok else None


def _start_point(branch: str, default: str) -> str:
    return (
        f"git rev-parse --verify --quiet refs/remotes/origin/{q(branch)} || "
        f"git rev-parse --verify --quiet refs/remotes/origin/{q(default)} || "
        f"git rev-parse --verify --quiet {q(default)}"
    )


def _has_branch(branch: str) -> str:
    return f"git rev-parse --verify --quiet refs/heads/{q(branch)} >/dev/null"


# One lock per project folder so opening, landing, and finishing never interleave.
_locks: dict[str, asyncio.Lock] = {}


def _lock(env: Environment, root: str) -> asyncio.Lock:
    return _locks.setdefault(f"{env.name}:{root}", asyncio.Lock())


async def ensure_home(env: Environment) -> None:
    home = env.workspace_root
    folders = " ".join(q(f"{home}/{name}") for name in HOME_FOLDERS)
    result = await env.exec(f"mkdir -p {folders}", timeout_s=30.0)
    if not result.ok:
        raise RuntimeError(
            f"Could not prepare the home folders: {result.describe_failure()}"
        )


async def prepare_project(
    env: Environment, project: str, *, github_token: str = ""
) -> bool:
    """Ensure `project` exists as a git repo; returns True if created by this call."""
    root = project_path(env.workspace_root, project)
    if is_code(project):
        create = _code_repo_script(repo_of(project), root)
    else:
        create = (
            f"mkdir -p {q(root)} && cd {q(root)} && git init --quiet -b {MAIN} && "
            f"git add -A && git commit --quiet --allow-empty -m {q(f'Start {project}')}"
        )
    exclude = " ".join(_EXCLUDED)
    script = (
        f"created=0\n"
        f"if [ ! -e {q(root)}/.git ]; then {{ {create}; }} || exit 21; created=1; fi\n"
        f"cd {q(root)} && ex=$(git rev-parse --git-path info/exclude) || exit 22\n"
        f'mkdir -p "$(dirname "$ex")" && for p in {exclude}; do '
        f'grep -qxF "$p" "$ex" 2>/dev/null || echo "$p" >> "$ex"; done\n'
        f'echo "__CREATED__$created"'
    )
    async with _lock(env, f"{root}#create"):
        result = await env.exec(
            script, env=git_env(github_token), timeout_s=300.0
        )
    if not result.ok:
        failure = result.describe_failure()
        if not github_token and "could not read Username" in failure:
            raise RuntimeError(
                f"Could not clone {project}: it looks private, and this thread has no "
                "GitHub token (see the warning above for why). Connect a "
                "fine-grained GitHub token or set up the GitHub App."
            )
        raise RuntimeError(f"Could not prepare {project}: {failure}")
    return "__CREATED__1" in result.stdout


def _code_repo_script(repo: str, root: str) -> str:
    git_url, upstream_url, default_branch, seed_files = repo_source(repo)
    upstream = (
        f" && git -C {q(root)} remote add upstream {q(upstream_url)}"
        if upstream_url
        else ""
    )
    if git_url:
        return f"git clone --quiet {q(git_url)} {q(root)}{upstream}"
    if repo not in {r["name"] for r in known_repos()}:
        return (
            f"echo {q(f'{repo} is not in the workspaces manifest')} >&2; false"
        )
    seed_cmds = []
    for raw_rel, content in seed_files.items():
        rel = raw_rel.lstrip("/")
        if "/" in rel:
            seed_cmds.append(f"mkdir -p {q(rel.rsplit('/', 1)[0])}")
        seed_cmds.append(f"printf %s {q(content)} > {q(rel)}")
    if not seed_cmds:
        seed_cmds.append(f"printf '# %s\\n' {q(repo)} > README.md")
    return (
        f"mkdir -p {q(root)} && cd {q(root)} && git init --quiet -b {q(default_branch)} && "
        + " && ".join(seed_cmds)
        + " && git add -A && git commit --quiet -m 'Seed repository'"
    )


def _lease_path(project: str) -> str:
    return f"{LEASES_DIR}/{project.replace('/', '__')}.json"


async def _lease_holder(env: Environment, project: str) -> str | None:
    try:
        content = (await env.read_file(_lease_path(project))).content
        return str(json.loads(content)["thread_id"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        return None


async def _take_lease(env: Environment, project: str, thread_id: str) -> None:
    lease = {"thread_id": thread_id, "since": datetime.now(UTC).isoformat()}
    await env.write_file(_lease_path(project), json.dumps(lease))


async def _release_lease(
    env: Environment, project: str, thread_id: str
) -> None:
    """Best-effort release; `open_workspace` treats leases held by finished threads as free."""
    try:
        if await _lease_holder(env, project) == thread_id:
            path = f"{env.workspace_root}/{_lease_path(project)}"
            await env.exec(f"rm -f {q(path)}", timeout_s=30.0)
    except RuntimeError:
        logger.warning(
            "Could not release %s's lease on %s",
            thread_id,
            project,
            exc_info=True,
        )


@dataclass
class Workspace:
    project: str
    thread_id: str
    root: str
    path: str  # root, or root/.worktrees/<thread_id>
    branch: str
    isolated: bool
    created: bool

    @property
    def thread_dir(self) -> str:
        return thread_dir(str(Path(self.root).parents[1]), self.thread_id)


async def open_workspace(
    env: Environment,
    project: str,
    thread_id: str,
    *,
    is_active: Callable[[str], bool],
    github_token: str = "",
) -> Workspace:
    """Lease `project` in place when free and clean, or allocate a git worktree."""
    created = await prepare_project(env, project, github_token=github_token)
    root = project_path(env.workspace_root, project)
    worktree = f"{root}/{WORKTREES_DIR}/{thread_id}"
    branch = thread_branch(thread_id)
    code = is_code(project)
    default = repo_source(repo_of(project))[2] if code else MAIN

    def workspace(path: str, branch: str, *, isolated: bool, new: bool):
        return Workspace(project, thread_id, root, path, branch, isolated, new)

    async with _lock(env, root):
        if (await env.exec(f"[ -e {q(worktree)}/.git ]")).ok:
            return workspace(worktree, branch, isolated=True, new=False)
        holder = await _lease_holder(env, project)
        free = holder is None or (holder != thread_id and not is_active(holder))
        if holder == thread_id or (free and await _is_clean(env, root)):
            if holder != thread_id:
                await _take_lease(env, project, thread_id)
            if not code:
                return workspace(root, MAIN, isolated=False, new=created)
            await _git_ok(
                env,
                root,
                f'[ "$(git symbolic-ref --short -q HEAD)" = {q(branch)} ] && exit 0\n'
                f"if {_has_branch(branch)}; then git switch --quiet {q(branch)}; exit; fi\n"
                "git fetch --quiet origin 2>/dev/null\n"
                f'start=$({_start_point(branch, default)}) && git switch --quiet -c {q(branch)} "$start"',
                f"switch {project} to {branch}",
                token=github_token,
            )
            return workspace(root, branch, isolated=False, new=created)

        start = (
            f"git fetch --quiet origin 2>/dev/null\nstart=$({_start_point(branch, default)}) || exit 3"
            if code
            else f"start={MAIN}"
        )
        await _git_ok(
            env,
            root,
            "git worktree prune\n"
            f"if {_has_branch(branch)}; then git worktree add --quiet {q(worktree)} {q(branch)}; exit; fi\n"
            f"{start}\n"
            f'git worktree add --quiet -b {q(branch)} {q(worktree)} "$start"',
            f"create a worktree for {thread_id} in {project}",
            token=github_token,
        )
        return workspace(worktree, branch, isolated=True, new=True)


async def _is_clean(env: Environment, path: str) -> bool:
    result = await _git(env, path, "git status --porcelain", timeout_s=30.0)
    return result.ok and not result.stdout.strip()


class LandingProblem(StrEnum):
    CONFLICT = "merge_conflict"
    BLOCKED = "merge_blocked"  # another thread's uncommitted edits overlap


@dataclass
class Finish:
    files: list[str] = field(default_factory=list)
    summary: str = ""
    raw_diff: str = ""
    sha: str | None = None
    landed: bool = True
    workdir: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    problem: LandingProblem | None = None
    problem_files: list[str] = field(default_factory=list)


async def finish_workspace(
    env: Environment,
    ws: Workspace,
    *,
    message: str,
    base: str | None = None,
) -> Finish:
    """Commit the iteration's changes, land non-code worktrees into `main`, and release the lease."""
    async with _lock(env, ws.root):
        before = await head_sha(env, ws.path)
        await _git_ok(
            env,
            ws.path,
            f"git add -A && {{ git diff --cached --quiet || git commit --quiet -m {q(message)}; }}",
            f"commit the work of {ws.thread_id}",
        )
        if is_code(ws.project):
            since = base or before
        elif ws.isolated:
            since = (
                await _git_ok(
                    env,
                    ws.path,
                    f"git merge-base {MAIN} HEAD",
                    "find the branch point",
                )
            ).strip()
        else:
            since = before
        finish = await collect_changes(env, ws.path, since or "HEAD")
        finish.artifacts = await read_artifacts(env, ws.path, finish.files)
        finish.workdir = ws.path
        if ws.isolated and not is_code(ws.project):
            finish.problem, finish.problem_files = await _land(env, ws)
            finish.landed = finish.problem is None
            if finish.landed:
                finish.workdir = ws.root
        if not ws.isolated:
            await _release_lease(env, ws.project, ws.thread_id)
            if is_code(ws.project):
                default = repo_source(repo_of(ws.project))[2]
                await _git_ok(
                    env,
                    ws.root,
                    f"git switch --quiet {q(default)}",
                    f"switch {ws.project} back to {default}",
                )
        if is_code(ws.project):
            # Root was switched back to the default branch; read the thread's branch tip.
            tip = await _git(
                env, ws.root, f"git rev-parse --verify {q(ws.branch)}"
            )
            finish.sha = tip.stdout.strip() if tip.ok else None
        else:
            finish.sha = await head_sha(env, finish.workdir)
        return finish


async def push_branch(
    env: Environment, project: str, branch: str, *, github_token: str = ""
) -> tuple[bool, str]:
    root = project_path(env.workspace_root, project)
    return await _push(env, root, branch, github_token)


async def _push(
    env: Environment, path: str, branch: str, token: str
) -> tuple[bool, str]:
    result = await _git(
        env,
        path,
        "git remote get-url origin >/dev/null 2>&1 || { echo __NO_REMOTE__; exit 0; }\n"
        f"if out=$(git push --quiet -u origin {q(branch)} 2>&1); then echo __PUSHED__; "
        'else echo "__PUSH_FAILED__ $out" | tail -c 400; fi',
        token=token,
    )
    out = result.stdout
    if result.error:
        return False, f"Push failed: {result.error}"
    if "__NO_REMOTE__" in out:
        return False, "No remote configured, so nothing was pushed."
    if "__PUSHED__" in out:
        return True, ""
    return False, "Push failed: " + out.partition("__PUSH_FAILED__")[2].strip()[
        :300
    ]


async def _land(
    env: Environment, ws: Workspace
) -> tuple[LandingProblem | None, list[str]]:
    merged = await _git(
        env, ws.path, f"git merge --quiet --no-edit {MAIN} >/dev/null 2>&1"
    )
    if not merged.ok:
        conflicts = await _git(
            env,
            ws.path,
            "git diff --name-only --diff-filter=U; git merge --abort >/dev/null 2>&1",
        )
        return LandingProblem.CONFLICT, _lines(conflicts.stdout)
    advanced = await _git(
        env,
        ws.root,
        f"git merge --quiet --ff-only {q(ws.branch)} >/dev/null 2>&1",
    )
    if not advanced.ok:
        # Uncommitted in-place edits in the root overlap with the incoming branch.
        listed = await _git(
            env,
            ws.root,
            f"git diff --name-only HEAD {q(ws.branch)}; echo __DIRTY__; "
            "git diff --name-only HEAD; git ls-files --others --exclude-standard",
        )
        incoming, _, dirty = listed.stdout.partition("__DIRTY__")
        overlap = sorted(set(_lines(incoming)) & set(_lines(dirty)))
        return LandingProblem.BLOCKED, overlap
    ignored_status = await _git(
        env, ws.path, "git status --ignored=traditional --porcelain"
    )
    ignored = [
        line[3:].strip()
        for line in ignored_status.stdout.splitlines()
        if line.startswith("!! ")
    ]
    if ignored:
        logger.warning(
            "Worktree %s in %s has ignored files before removal: %s",
            ws.thread_id,
            ws.project,
            ", ".join(ignored),
        )
    await _git_ok(
        env,
        ws.root,
        f"git worktree remove --force {q(ws.path)} && git branch --quiet -D {q(ws.branch)}",
        f"remove the worktree of {ws.thread_id}",
    )
    return None, []


async def collect_changes(env: Environment, path: str, since: str) -> Finish:
    rev = q(since)
    stdout = await _git_ok(
        env,
        path,
        f"echo __FILES__; git diff --name-only {rev} HEAD; "
        f"echo __NUMSTAT__; git diff --numstat {rev} HEAD; "
        f"echo __DIFF__; git diff {rev} HEAD | head -c {_MAX_RAW_DIFF_BYTES}",
        f"inspect the changes in {path}",
        timeout_s=60.0,
    )
    _, _, rest = stdout.partition("__FILES__\n")
    files_text, _, rest = rest.partition("__NUMSTAT__\n")
    numstat_text, _, raw_diff = rest.partition("__DIFF__\n")
    files = sorted(_lines(files_text))
    if not files:
        return Finish()
    added = deleted = 0
    for row in numstat_text.splitlines():
        cols = row.split("\t")
        if len(cols) >= 2:
            added += int(cols[0]) if cols[0].isdigit() else 0
            deleted += int(cols[1]) if cols[1].isdigit() else 0
    return Finish(
        files=files,
        summary=f"{len(files)} file(s) changed (+{added} -{deleted})",
        raw_diff=raw_diff,
    )


async def read_artifacts(
    env: Environment, path: str, files: list[str]
) -> list[dict[str, str]]:
    candidates = [
        f
        for f in files
        if Path(f).suffix.lower() in _ARTIFACT_EXTENSIONS
        and Path(f).name.lower() not in _IGNORED_ARTIFACTS
    ][:4]
    artifacts = []
    for rel in candidates:
        result = await env.exec(
            f"head -c 14000 {q(f'{path}/{rel}')}", timeout_s=30.0
        )
        text = result.stdout.strip() if result.ok else ""
        if text:
            artifacts.append(
                {
                    "name": rel,
                    "title": _artifact_title(rel, text),
                    "content": text,
                }
            )
    return artifacts


def _artifact_title(rel_path: str, content: str) -> str:
    for line in content.splitlines()[:6]:
        stripped = line.strip()
        if stripped.startswith("# ") and stripped[2:].strip():
            return stripped[2:].strip().strip("*_`")[:64]
    stem = Path(rel_path).stem.replace("_", " ").replace("-", " ").strip()
    return stem.title() if stem else Path(rel_path).name


async def clear_thread_dir(env: Environment, thread_id: str) -> None:
    await env.exec(
        f"rm -rf {q(thread_dir(env.workspace_root, thread_id))}", timeout_s=30.0
    )


class PromoteError(ValueError):
    pass


_PROMOTE_REFUSALS = {
    3: "Its files are gone: one-offs are cleared after 30 quiet days.",
    4: "A project with that name already exists.",
    5: "A thread is still working in it.",
}


async def promote_scratch(env: Environment, scratch: str, project: str) -> None:
    """Move `scratch/<name>` to `projects/<slug>` when no worktree is open in it."""
    if (
        project_kind(scratch) is not ProjectKind.SCRATCH
        or project_kind(project) is not ProjectKind.PROJECT
    ):
        raise PromoteError("Only a one-off can be kept as a project.")
    home = env.workspace_root
    src, dst = project_path(home, scratch), project_path(home, project)
    result = await env.exec(
        f"[ -d {q(src)} ] || exit 3\n"
        f"[ -e {q(dst)} ] && exit 4\n"
        f'[ -n "$(ls -A {q(f"{src}/.worktrees")} 2>/dev/null)" ] && exit 5\n'
        f"mkdir -p {q(f'{home}/{ProjectKind.PROJECT}')} && mv {q(src)} {q(dst)} || exit 1\n"
        f"rm -f {q(f'{home}/{_lease_path(scratch)}')}\n"
        f"git -C {q(dst)} worktree prune 2>/dev/null || true",
        timeout_s=60.0,
    )
    if result.ok:
        return
    if result.error is None and result.exit_code in _PROMOTE_REFUSALS:
        raise PromoteError(_PROMOTE_REFUSALS[result.exit_code])
    raise PromoteError(f"Couldn't move it: {result.describe_failure()}")


async def prune_scratch(
    env: Environment,
    *,
    older_than_days: int = 30,
    keep: tuple[str, ...] | list[str] | set[str] = (),
) -> list[str]:
    """Delete scratch folders nothing in has changed for `older_than_days`; returns them.

    A folder moved to `projects/` is no longer scratch, so it is kept; so is one a thread
    is working in (it holds a lease) or a named one-off waiting to move (`keep`).
    """
    home = env.workspace_root
    leases = f"{home}/{LEASES_DIR}/scratch__"
    kept = {
        item.removeprefix("scratch/").strip("/")
        for item in keep
        if item and item.removeprefix("scratch/").strip("/")
    }
    skip = (
        '  case "$d" in '
        + "|".join(q(name) for name in sorted(kept))
        + ") continue ;; esac\n"
        if kept
        else ""
    )
    result = await env.exec(
        f"cd {q(f'{home}/scratch')} 2>/dev/null || exit 0\n"
        'for d in */; do d="${d%/}"; [ -d "$d" ] || continue\n'
        f"{skip}"
        f'  [ -e {q(leases)}"$d.json" ] && continue\n'
        f'  [ -n "$(find "$d" -mtime -{int(older_than_days)} -print 2>/dev/null | head -n 1)" ] && continue\n'
        '  rm -rf "$d" && echo "$d"\n'
        "done",
        timeout_s=120.0,
    )
    if not result.ok:
        logger.warning("Could not prune scratch: %s", result.describe_failure())
    return [f"scratch/{name}" for name in _lines(result.stdout)]
