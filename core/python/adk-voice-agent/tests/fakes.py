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

"""Test doubles injected at the PATH and environment seams."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.store.thread_store import ThreadStore
from app.threads.model import ThreadHandle
from app.threads.registry import ThreadRegistry
from app.workers import runs
from app.workers.environment import ExecResult, LocalEnvironment

FAKE_AGENT_CLI = Path(__file__).with_name("fake_agent_cli.py")

REPO = "voice-agent-fixture"
GOAL = "Add a greet() helper that returns a greeting"


async def wait_until(
    predicate: Callable[[], bool | Awaitable[bool]],
    timeout_s: float = 10.0,
    interval_s: float = 0.02,
    message: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        outcome = predicate()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out after {timeout_s}s waiting for {message}"
            )
        await asyncio.sleep(interval_s)


def kinds(handle: ThreadHandle) -> list[str]:
    return [e.kind for e in handle.events]


def git(clone: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", clone, *args], capture_output=True, text=True, check=True
    ).stdout


async def seed_thread(store: ThreadStore, handle: ThreadHandle) -> ThreadHandle:
    await store.save_thread(handle.to_dict())
    for event in handle.events:
        await store.append_event(handle.thread_id, event.to_dict())
    return handle


class FakeAgent:
    """Scripted stand-in for `claude` and `agy` CLIs (`tests/fake_agent_cli.py`)."""

    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "bin"
        self.state_dir = root / "state"
        self.bin_dir.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        for name, dialect in (("claude", "claude"), ("agy", "agy")):
            wrapper = self.bin_dir / name
            wrapper.write_text(
                f'#!/bin/sh\nexec {sys.executable} {FAKE_AGENT_CLI} {dialect} "$@"\n'
            )
            wrapper.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_AGENT_STATE": str(self.state_dir),
        }
        self.script()

    def script(self, *turns: dict[str, Any]) -> None:
        (self.state_dir / "turns.json").write_text(json.dumps(list(turns)))
        (self.state_dir / "counter").unlink(missing_ok=True)

    def scenarios(self, by_repo: dict[str, list[dict[str, Any]]]) -> None:
        (self.state_dir / "scenarios.json").write_text(json.dumps(by_repo))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self._read("calls.jsonl")

    @property
    def commands(self) -> list[dict[str, Any]]:
        return self._read("commands.jsonl")

    @property
    def mcp_calls(self) -> list[dict[str, Any]]:
        return self._read("mcp_calls.jsonl")

    def _read(self, name: str) -> list[dict[str, Any]]:
        path = self.state_dir / name
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )


@dataclass
class Lab:
    """Hermetic thread stack with scripted model."""

    env: LocalEnvironment
    agent: FakeAgent
    registry: ThreadRegistry
    store: ThreadStore
    state_dir: Path

    def set_policy(self, **run_budget_min: float) -> None:
        import yaml

        (self.state_dir / "policy.yaml").write_text(
            yaml.safe_dump({"run_budget": run_budget_min})
        )

    async def dispatch(self, goal: str = GOAL, repo: str = REPO, **kwargs: Any):
        handle = await self.registry.dispatch(goal, repo, **kwargs)
        return await self.settle(handle.thread_id)

    async def settle(
        self, thread_id: str, timeout_s: float = 20.0
    ) -> ThreadHandle:
        handle = await self.registry.require(thread_id)
        if handle.driver is not None:
            await asyncio.wait_for(asyncio.shield(handle.driver), timeout_s)
        return handle

    async def wait_for_calls(self, count: int, timeout_s: float = 10.0) -> None:
        await wait_until(
            lambda: len(self.agent.calls) >= count,
            timeout_s,
            message=f"{count} harness process start(s)",
        )

    def exit_code(self, run_id: str) -> int:
        status = Path(runs.run_dir_for(self.env, run_id)) / "status.json"
        return json.loads(status.read_text())["exit_code"]


@contextlib.asynccontextmanager
async def serve_mcp() -> AsyncIterator[str]:
    """Serve `app.api.mcp` on a free local port and yield its base URL."""
    import uvicorn
    from fastapi import FastAPI

    from app.api.mcp import router

    app = FastAPI()
    app.include_router(router)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, ws="none", log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve())
    try:
        await wait_until(lambda: server.started, message="the MCP server")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


@dataclass
class ScriptedSandbox:
    """Managed environment that records commands and simulates `test -f` / `touch`."""

    name: str = "sandboxes/scripted-1"
    workspace_root: str = "/workspace"
    managed: bool = True
    failing: tuple[str, ...] = ()
    files: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)

    async def exec(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> ExecResult:
        self.commands.append(command)
        if command.startswith("test -f "):
            return ExecResult(0 if shlex.split(command)[2] in self.files else 1)
        if "touch " in command:
            self.files.add(shlex.split(command.rsplit("touch ", 1)[1])[0])
            return ExecResult(0)
        if any(marker in command for marker in self.failing):
            return ExecResult(1, stderr="install failed")
        return ExecResult(0)

    def credential_env(self) -> dict[str, str]:
        return {}


def app_client_env(*names: str) -> dict[str, str]:
    from app.auth.github.api import GITHUB_PROVIDER
    from app.auth.github.provider import GITHUB_APP_SLUG_ENV
    from app.auth.providers import all_providers

    env: dict[str, str] = {}
    for name, app in all_providers().items():
        if names and name not in names:
            continue
        env[app.client_id_env] = f"{name}-client-id"
        env[app.client_secret_env] = f"{name}-client-secret"
        if name == GITHUB_PROVIDER:
            env[GITHUB_APP_SLUG_ENV] = "voice-agent-test"
    return env


def configure_apps(monkeypatch: Any, *names: str) -> None:
    for key, value in app_client_env(*names).items():
        monkeypatch.setenv(key, value)


class InMemoryCredentialStore:
    per_user = True

    def __init__(self) -> None:
        self.users: dict[str, dict[str, str]] = {}

    def get_all(self, user_id: str) -> dict[str, str]:
        return dict(self.users.get(user_id, {}))

    def get(self, user_id: str, key: str) -> str:
        return self.users.get(user_id, {}).get(key, "").strip()

    def set_many(self, user_id: str, updates: Mapping[str, str | None]) -> bool:
        tokens = self.users.setdefault(user_id, {})
        for key, value in updates.items():
            if value is None:
                tokens.pop(key, None)
            else:
                tokens[key] = value
        return True

    def delete(self, user_id: str, keys: Any) -> bool:
        return self.set_many(user_id, dict.fromkeys(keys))

    def invalidate(self, user_id: str) -> None:
        pass


@dataclass
class _SecretPayload:
    data: bytes


@dataclass
class _SecretVersion:
    name: str
    payload: _SecretPayload


class FakeSecretManagerClient:
    """In-memory `SecretManagerServiceClient` installed by `tests/conftest.py`."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def create_secret(self, request: dict[str, Any]) -> None:
        from google.api_core.exceptions import AlreadyExists

        self.calls.append("create_secret")
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        if name in self.secrets:
            raise AlreadyExists(name)
        self.secrets[name] = {
            "labels": dict(request["secret"].get("labels") or {}),
            "versions": {},
        }

    def add_secret_version(self, request: dict[str, Any]) -> _SecretVersion:
        from google.api_core.exceptions import NotFound

        self.calls.append("add_secret_version")
        secret = self.secrets.get(request["parent"])
        if secret is None:
            raise NotFound(request["parent"])
        number = len(secret["versions"]) + 1
        name = f"{request['parent']}/versions/{number}"
        secret["versions"][number] = request["payload"]["data"]
        return _SecretVersion(name, _SecretPayload(request["payload"]["data"]))

    def access_secret_version(self, request: dict[str, Any]) -> _SecretVersion:
        from google.api_core.exceptions import NotFound

        self.calls.append("access_secret_version")
        parent, _, version = request["name"].rpartition("/versions/")
        secret = self.secrets.get(parent)
        live = {
            n: d for n, d in (secret or {}).get("versions", {}).items() if d
        }
        if not live:
            raise NotFound(request["name"])
        number = max(live) if version == "latest" else int(version)
        if number not in live:
            raise NotFound(request["name"])
        return _SecretVersion(
            f"{parent}/versions/{number}", _SecretPayload(live[number])
        )

    def destroy_secret_version(self, request: dict[str, Any]) -> None:
        self.calls.append("destroy_secret_version")
        parent, _, version = request["name"].rpartition("/versions/")
        self.secrets[parent]["versions"][int(version)] = b""

    def blob(self, secret_name: str) -> dict[str, str]:
        versions = self.secrets.get(secret_name, {}).get("versions", {})
        live = [d for d in versions.values() if d]
        return json.loads(live[-1]) if live else {}


class FakeMemoryStore:
    """In-memory `MemoryStore` with optional `delay_s` to exercise recall timeouts."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.users: dict[str, dict[str, tuple[str, Any]]] = {}
        self.generated: list[tuple[str, list[Any]]] = []
        self.labels: list[dict[str, str] | None] = []
        self.delay_s = delay_s

    def set_profile(self, user_id: str, **fields: Any) -> None:
        self.users[user_id] = {
            name: (
                f"mem-{user_id}-{name}".replace("@", "-").replace(".", "-"),
                value,
            )
            for name, value in fields.items()
        }

    async def profile(self, user_id: str) -> dict[str, Any]:
        await asyncio.sleep(self.delay_s)
        return {k: v for k, (_, v) in self.users.get(user_id, {}).items()}

    async def fields(self, user_id: str) -> list[Any]:
        from app.memory.store import ProfileField

        return [
            ProfileField(id=memory_id, field=name, value=value, updated_at=1.0)
            for name, (memory_id, value) in self.users.get(user_id, {}).items()
        ]

    async def delete_field(self, user_id: str, memory_id: str) -> bool:
        fields = self.users.get(user_id, {})
        for name, (mid, _) in list(fields.items()):
            if mid == memory_id:
                del fields[name]
                return True
        return False

    async def generate(
        self,
        user_id: str,
        contents: Any,
        revision_labels: dict[str, str] | None = None,
    ) -> None:
        self.generated.append((user_id, list(contents)))
        self.labels.append(revision_labels)


class FakeEpisodeStore:
    """In-memory `EpisodeStore` with word-overlap search and optional `delay_s`."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.memories: dict[tuple[str, str], dict[str, Any]] = {}
        self.delay_s = delay_s

    async def put(
        self,
        user_id: str,
        kind: str,
        memory_id: str,
        fact: str,
        metadata: dict[str, Any],
        *,
        ttl_s: float | None = None,
    ) -> None:
        from app.memory.episodes import Episode

        self.memories.setdefault((user_id, kind), {})[memory_id] = Episode(
            id=memory_id,
            kind=kind,
            fact=fact,
            created_at=time.time(),
            metadata=dict(metadata),
        )

    def all(self, user_id: str, kind: str) -> list[Any]:
        return sorted(
            self.memories.get((user_id, kind), {}).values(),
            key=lambda e: e.created_at or 0.0,
            reverse=True,
        )

    async def search(
        self, user_id: str, kind: str, query: str, *, top_k: int
    ) -> list[Any]:
        await asyncio.sleep(self.delay_s)
        words = {w for w in query.lower().split() if len(w) > 2}
        return [
            e
            for e in self.all(user_id, kind)
            if words & set(e.fact.lower().split())
        ][:top_k]

    async def latest(self, user_id: str, kind: str, *, limit: int) -> list[Any]:
        return self.all(user_id, kind)[:limit]

    async def delete(self, user_id: str, kind: str, memory_id: str) -> bool:
        return (
            self.memories.get((user_id, kind), {}).pop(memory_id, None)
            is not None
        )

    async def delete_all(self, user_id: str, kind: str) -> int:
        return len(self.memories.pop((user_id, kind), {}))
