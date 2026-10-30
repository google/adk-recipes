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

"""Runs one System 2 case end to end on a docker computer and reports what happened."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tarfile
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from app.auth.identity import run_as
from app.threads.model import (
    FINISHED_STATES,
    EventKind,
    Instruction,
    InstructionKind,
    ThreadEvent,
    ThreadHandle,
    ThreadState,
)

from . import snapshot
from .fake_apps import FakeApps, Mailbox, RoutingTransport
from .report import summarize_usage

logger = logging.getLogger(__name__)

COLLECT_SCRIPT = Path(__file__).with_name("collect.py")
HOSTED_MCP_JSON = Path(__file__).parents[1] / "fixtures" / "hosted.mcp.json"
DEFAULT_TIMEOUT_S = 20 * 60
STOP_STATES = FINISHED_STATES | {
    ThreadState.AWAITING_PERMISSION,
}
_RESULT_KINDS = {
    EventKind.COMPLETED,
    EventKind.ERROR,
    EventKind.PERMISSION_REQUESTED,
    EventKind.CANCELLED,
}

APPS = FakeApps()
_installed: dict[str, Any] = {}


def install() -> None:
    """Point the MCP gateway at `HOSTED_MCP_JSON` so HTTP transport intercepts Google & GitHub upstreams."""
    if _installed:
        return
    from app.api import mcp_gateway
    from app.app_utils.http_client import set_default_http_transport
    from app.auth.credential_store import set_credential_store
    from app.memory.store import set_memory_store
    from tests.fakes import FakeMemoryStore, InMemoryCredentialStore

    creds, memory = InMemoryCredentialStore(), FakeMemoryStore()
    mcp_gateway.MCP_JSON_PATH = HOSTED_MCP_JSON
    set_default_http_transport(RoutingTransport(APPS))
    set_credential_store(creds)
    set_memory_store(memory)
    _installed.update(creds=creds, memory=memory)


async def _docker(
    *args: str, stdin: bytes | None = None, check: bool = True
) -> str:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE
        if stdin is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"docker {args[0]} failed: {err.decode(errors='replace')[:400]}"
        )
    return out.decode(errors="replace")


def _tar(home: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(home.rglob("*")):
            tar.add(
                path, arcname=path.relative_to(home).as_posix(), recursive=False
            )
    return buf.getvalue()


async def restore_volume(name: str, home: Path, image: str) -> None:
    await _docker("rm", "--force", name, check=False)
    await _docker("volume", "rm", "--force", name, check=False)
    await _docker("volume", "create", "--label", "goog-adk-eval=true", name)
    await _docker(
        "run", "--rm", "-i", "--volume", f"{name}:{snapshot.CONTAINER_HOME}",
        "--entrypoint", "sh", image, "-c",
        f"tar -C {snapshot.CONTAINER_HOME} -xf - && chown -R 1000:1000 {snapshot.CONTAINER_HOME}",
        stdin=_tar(home),
    )  # fmt: skip


async def remove_computer(name: str) -> None:
    await _docker("rm", "--force", name, check=False)
    await _docker("volume", "rm", "--force", name, check=False)


def _journal_thread(
    persona: snapshot.Persona, alias: str, owner: str, harness: str
) -> ThreadHandle:
    spec = persona.render(persona.journal[alias])
    ended = time.time() - 86400 * int(spec.get("days_ago", 0)) - 3600
    return ThreadHandle(
        thread_id=persona.ids[alias],
        owner=owner,
        title=spec["title"],
        goal=spec["goal"],
        project=spec["project"],
        harness=harness,
        branch=f"agent/{persona.ids[alias]}",
        state=ThreadState.COMPLETED,
        summary=spec.get("summary", ""),
        result_text=spec.get("result_text", ""),
        files_changed=list(spec.get("files_changed") or []),
        instructions=[Instruction(InstructionKind.GOAL, spec["goal"], 1)],
        created_at=ended - 600,
        updated_at=ended,
        ended_at=ended,
    )


async def seed_user(
    persona: snapshot.Persona, owner: str, journal: list[str], harness: str
) -> list[str]:
    from app.store.thread_store import get_thread_store

    store = get_thread_store()
    for alias in journal:
        await store.save_thread(
            _journal_thread(persona, alias, owner, harness).to_dict()
        )
    await store.mark_all_seen(owner)
    _installed["memory"].set_profile(owner, **persona.data.get("profile", {}))
    tokens = {
        "GOOGLE_WORKSPACE_ACCESS_TOKEN": f"fake-workspace-{owner}",
        "GITHUB_ACCESS_TOKEN": f"fake-github-{owner}",
    }
    _installed["creds"].set_many(owner, tokens)
    return list(tokens.values())


async def _collect(env: Any, *args: str) -> Any:
    script = base64.b64encode(COLLECT_SCRIPT.read_bytes()).decode()
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in args)
    result = await env.exec(
        f"echo {script} | base64 -d > /tmp/eval-collect.py && python3 /tmp/eval-collect.py {quoted}",
        timeout_s=300.0,
    )
    if not result.ok:
        raise RuntimeError(
            f"collect {args[0]} failed: {result.describe_failure()}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def diff_files(
    before: dict[str, str], after: dict[str, str]
) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "modified": sorted(
            p for p in after if p in before and after[p] != before[p]
        ),
        "deleted": sorted(set(before) - set(after)),
    }


def diff_repos(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for path, repo in after.items():
        old = before.get(path) or {"commits": [], "branches": {}}
        seen = {c["sha"] for c in old["commits"]}
        new = [c for c in repo["commits"] if c["sha"] not in seen]
        moved = {
            b: [old["branches"].get(b), sha]
            for b, sha in repo["branches"].items()
            if old["branches"].get(b) != sha
        }
        out[path] = {
            "created": path not in before,
            "new_commits": [
                {"subject": c["subject"], "merge": " " in c.get("parents", "")}
                for c in new
            ],
            "branches_moved": moved,
            "main_commits": _count_on(repo, "main") - _count_on(old, "main"),
            "head": repo["head"],
            "status": repo["status"][:30],
            "worktrees": repo["worktrees"],
        }
    for path in before:
        if path not in after:
            out[path] = {"deleted": True}
    return out


def _count_on(repo: dict[str, Any], branch: str) -> int:
    by_sha = {c["sha"]: c for c in repo.get("commits", [])}
    tip = (repo.get("branches") or {}).get(branch)
    seen, stack = set(), [tip] if tip else []
    while stack:
        sha = stack.pop()
        if sha in seen or sha not in by_sha:
            continue
        seen.add(sha)
        stack.extend(by_sha[sha].get("parents", "").split())
    return len(seen)


def diff_remotes(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    return {
        name: {
            ref: [before.get(name, {}).get(ref), sha]
            for ref, sha in refs.items()
            if before.get(name, {}).get(ref) != sha
        }
        for name, refs in after.items()
    }


def thread_timings(
    events: list[ThreadEvent], started: float
) -> dict[str, float | None]:
    def first(
        pred: Callable[[ThreadEvent], bool], after: float = started
    ) -> float | None:
        ts = next((e.ts for e in events if e.ts >= after and pred(e)), None)
        return None if ts is None else round(ts - started, 2)

    def to_state(*states: str) -> Callable[[ThreadEvent], bool]:
        return lambda e: (
            e.kind is EventKind.STATE_CHANGED
            and (e.data or {}).get("to") in states
        )

    harness = first(to_state("executing"))
    return {
        "computer_ready_s": first(
            lambda e: (
                e.kind is EventKind.PROGRESS
                and e.message.startswith("Preparing")
            )
        ),
        "harness_start_s": harness,
        "first_update_s": None
        if harness is None
        else first(
            lambda e: (
                e.kind in (EventKind.PROGRESS, EventKind.ACTIVITY)
                and not e.message.startswith("Preparing")
            ),
            after=started + harness,
        ),
        # Time to first useful update: the computer's approach line (`update_user`).
        "approach_s": first(
            lambda e: (
                e.kind is EventKind.PROGRESS
                and bool((e.data or {}).get("finding"))
            )
        ),
        "execute_start_s": first(to_state("executing")),
        "result_s": first(lambda e: e.kind in _RESULT_KINDS),
    }


def _event_dict(e: ThreadEvent, started: float) -> dict[str, Any]:
    out: dict[str, Any] = {
        "t": round(e.ts - started, 1),
        "kind": e.kind.value,
        "message": e.message[:400],
    }
    if e.kind is EventKind.PROGRESS and (e.data or {}).get("finding"):
        out["finding"] = True
    if e.kind is EventKind.PERMISSION_REQUESTED:
        out["data"] = {
            k: e.data.get(k) for k in ("command", "action_class", "kind")
        }
    return out


class CaseRun:
    def __init__(
        self,
        spec: dict[str, Any],
        *,
        harness: str,
        on_event: Callable[[str, ThreadEvent], None] | None = None,
        today: date | None = None,
    ) -> None:
        self.spec = spec
        self.case_id = str(spec.get("case") or "case")
        self.harness = harness
        self.on_event = on_event
        self.persona = snapshot.load_persona(
            spec.get("persona", "alex"), today=today
        )
        stamp = uuid.uuid4().hex[:6]
        self.owner = f"eval-{self.case_id}-{harness}-{stamp}"
        self.events: dict[str, list[ThreadEvent]] = {}
        self.started: dict[str, float] = {}
        self.handles: dict[str, ThreadHandle] = {}

    @property
    def image(self) -> str:
        from app.workers.providers.docker import DEFAULT_COMPUTER_IMAGE

        return os.getenv("COMPUTER_IMAGE") or DEFAULT_COMPUTER_IMAGE

    def _listener(self, handle: ThreadHandle, event: ThreadEvent) -> None:
        alias = next(
            (
                a
                for a, h in self.handles.items()
                if h.thread_id == handle.thread_id
            ),
            None,
        )
        if alias is None:
            return
        self.events.setdefault(alias, []).append(event)
        if self.on_event is not None:
            self.on_event(alias, event)

    async def run(self, work_dir: Path) -> dict[str, Any]:
        from app.threads.registry import get_thread_registry
        from app.workers.factory import (
            invalidate_environment,
            resolve_environment,
        )
        from app.workers.providers.common import owner_id
        from app.workers.providers.docker import computer_name
        from app.workers.providers.registry import get_computer_provider

        install()
        report: dict[str, Any] = {
            "case": self.case_id,
            "harness": self.harness,
            "owner": self.owner,
            "journal_ids": dict(self.persona.ids),
            "projects": self.persona.projects(),
            "today": self.persona.today.isoformat(),
            "steps": self.spec.get("steps"),
        }
        name = computer_name(owner_id(self.owner))
        registry = get_thread_registry()
        tokens: list[str] = []
        mailbox = Mailbox(
            self.persona.data.get("apps") or {}, self.persona.today
        )
        t0 = time.time()
        try:
            with run_as(self.owner):
                journal = snapshot.included_journal(
                    self.persona,
                    omit=self.spec.get("omit") or (),
                    extra=self.spec.get("with_journal") or (),
                )
                home = work_dir / "home"
                snapshot.build_home(
                    self.persona,
                    home,
                    omit=self.spec.get("omit") or (),
                    extra=self.spec.get("with_journal") or (),
                )
                await restore_volume(name, home, self.image)
                tokens = await seed_user(
                    self.persona, self.owner, journal, self.harness
                )
                APPS.register(tokens, mailbox)
                report["journal"] = journal

                boot = time.time()
                await get_computer_provider().ensure(self.owner)
                report["computer"] = {
                    "image": self.image,
                    "boot_s": round(time.time() - boot, 2),
                }
                env = (await resolve_environment()).env
                baseline = await _collect(env, "snapshot")

                registry.add_listener(self._listener, owner=self.owner)
                try:
                    await self._drive(registry)
                finally:
                    registry.remove_listener(self._listener)

                final = await _collect(env, "snapshot")
                report.update(await self._collect_outcome(env, baseline, final))
                report["usage_runs"] = await _collect(env, "usage")
                report["usage"] = summarize_usage(report["usage_runs"])
        except Exception as exc:
            logger.exception("Case %s on %s failed", self.case_id, self.harness)
            report["runner_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            shutil.rmtree(work_dir / "home", ignore_errors=True)
            APPS.forget(tokens)
            report["threads"] = {
                alias: self._thread_report(alias) for alias in self.handles
            }
            report["gateway"] = {
                "calls": [
                    {**c, "t": round(c["ts"] - t0, 1), "ts": None}
                    for c in mailbox.calls
                ],
                "drive_files": mailbox.drive_files(),
                "permission_requests": [
                    {
                        "thread": alias,
                        **_event_dict(e, self.started.get(alias, t0)),
                    }
                    for alias, events in self.events.items()
                    for e in events
                    if e.kind is EventKind.PERMISSION_REQUESTED
                ],
            }
            report["wall_s"] = round(time.time() - t0, 1)
            invalidate_environment(owner_id(self.owner))
            if not os.getenv("SYSTEM2_KEEP_COMPUTERS"):
                await remove_computer(name)
        return report

    async def _drive(self, registry: Any) -> None:
        steps = self.spec.get("steps") or []
        if self.spec.get("parallel"):
            for step in steps:
                await self._start(registry, step)
            await self._watch(registry, [s["as"] for s in steps])
            return
        for step in steps:
            await self._start(registry, step)
            await self._watch(registry, [step["as"]])

    async def _start(self, registry: Any, step: dict[str, Any]) -> None:
        alias = step["as"]
        self.started[alias] = time.time()
        if "steer" in step:
            steer = self.persona.render(step["steer"])
            handle = await registry.require(steer["thread"])
            self.handles[alias] = handle
            await registry.steer(
                handle.thread_id, steer["instruction"], harness=self.harness
            )
            return
        dispatch = self.persona.render(step["dispatch"])
        handle = await registry.dispatch(
            dispatch["goal"],
            dispatch.get("project", ""),
            harness=self.harness,
            title=dispatch.get("title", ""),
        )
        self.handles[alias] = handle
        # Events emitted before the handle was known (the creation) are on the handle.
        self.events[alias] = list(handle.events)

    async def _watch(self, registry: Any, aliases: list[str]) -> None:
        timeout = float(self.spec.get("timeout_s") or DEFAULT_TIMEOUT_S)
        deadline = time.time() + timeout
        while True:
            waiting = []
            for alias in aliases:
                handle = self.handles[alias]
                if handle.state not in STOP_STATES:
                    waiting.append(alias)
            if not waiting:
                return
            if time.time() > deadline:
                for alias in waiting:
                    self.handles[
                        alias
                    ].error = f"timed out after {timeout:.0f}s"
                    try:
                        await registry.cancel(self.handles[alias].thread_id)
                    except Exception:
                        logger.warning(
                            "Could not cancel %s", alias, exc_info=True
                        )
                return
            await asyncio.sleep(1.0)

    async def _collect_outcome(
        self, env: Any, baseline: dict[str, Any], final: dict[str, Any]
    ) -> dict[str, Any]:
        changes = diff_files(baseline["files"], final["files"])
        repos = diff_repos(baseline["repos"], final["repos"])
        remotes = diff_remotes(baseline["remotes"], final["remotes"])
        read = [*changes["added"], *changes["modified"]][:40]
        for path in self.spec.get("read") or []:
            rendered = self.persona.render(path)
            if rendered in final["files"] and rendered not in read:
                read.append(rendered)
        tests, diffs = [], []
        for path, repo in repos.items():
            if path.startswith("code/") and not repo.get("deleted"):
                tests.append([path, "main"])
                main = (
                    (baseline["repos"].get(path) or {})
                    .get("branches", {})
                    .get("main", "main")
                )
                for branch, (old, _) in repo["branches_moved"].items():
                    if branch.startswith("agent/"):
                        tests.append([path, branch])
                        diffs.append([path, old or main, branch])
        request = {"read": read, "tests": tests, "diffs": diffs}
        inspected = await _collect(env, "inspect", json.dumps(request))
        return {
            "changes": changes,
            "repos": repos,
            "remotes": remotes,
            "texts": inspected["texts"],
            "tests": inspected["tests"],
            "diffs": inspected["diffs"],
            "tree": sorted(final["files"])[:2000],
        }

    def _thread_report(self, alias: str) -> dict[str, Any]:
        h = self.handles[alias]
        started = self.started.get(alias, h.created_at)
        events = self.events.get(alias, [])
        return {
            "thread_id": h.thread_id,
            "state": h.state.value,
            "project": h.project,
            "title": h.title,
            "iteration": h.iteration,
            "summary": h.summary,
            "result_text": (h.result_text or "")[:6000],
            "error": h.error,
            "permission": h.permission.to_dict() if h.permission else None,
            "files_changed": h.files_changed,
            "branch": h.branch,
            "pushed": h.pushed,
            "commit_sha": h.commit_sha,
            "timings": thread_timings(events, started),
            "events": [_event_dict(e, started) for e in events][-80:],
        }
