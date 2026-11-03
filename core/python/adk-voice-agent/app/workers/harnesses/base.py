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

"""Harness protocol (`CodingHarness`, `CliHarness`) and normalized `HarnessEvent` stream."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from app.threads.model import PermissionRequest
from app.workers import runs
from app.workers.environment import Environment

THREAD_CLONE_ENV = "VOICE_AGENT_THREAD_CLONE"
THREAD_CONTEXT_FILE = "context.md"
MCP_CONFIG_FILE = "mcp.json"
MCP_SERVER_NAME = "voice_agent"
MCP_URL_ENV = "VOICE_AGENT_MCP_URL"
MCP_TOKEN_ENV = "VOICE_AGENT_MCP_TOKEN"  # noqa: S105 (a variable name, not a secret)
WEB_SEARCH_VERB = "Searching the web for"


@dataclass
class RunRequest:
    thread_id: str
    run_id: str
    prompt: str
    system_prompt: str
    cwd: str
    session_id: str | None
    resume: bool
    thread_dir: str = ""
    env: dict[str, str] = field(default_factory=dict)
    path_prepend: list[str] = field(default_factory=list)


class HarnessEventKind(StrEnum):
    SESSION = "session"
    PROGRESS = "progress"
    ACTIVITY = "activity"
    PERMISSION = "permission"
    RESULT = "result"


@dataclass(frozen=True)
class HarnessResult:
    text: str
    is_error: bool = False
    retryable: bool = False


@dataclass(frozen=True)
class HarnessEvent:
    kind: HarnessEventKind
    message: str = ""
    session_id: str | None = None
    result: HarnessResult | None = None
    permission: PermissionRequest | None = None

    @classmethod
    def session(cls, session_id: str) -> HarnessEvent:
        return cls(HarnessEventKind.SESSION, session_id=str(session_id))

    @classmethod
    def progress(cls, message: str) -> HarnessEvent:
        return cls(HarnessEventKind.PROGRESS, message=message)

    @classmethod
    def activity(cls, message: str) -> HarnessEvent:
        return cls(HarnessEventKind.ACTIVITY, message=message)

    @classmethod
    def permission_requested(
        cls, permission: PermissionRequest
    ) -> HarnessEvent:
        return cls(
            HarnessEventKind.PERMISSION,
            message=permission.command,
            permission=permission,
        )

    @classmethod
    def finished(cls, result: HarnessResult) -> HarnessEvent:
        return cls(
            HarnessEventKind.RESULT,
            message=first_line(result.text),
            result=result,
        )


@dataclass
class HarnessPoll:
    events: list[HarnessEvent] = field(default_factory=list)
    exit_code: int | None = None
    output_seen: bool = False
    error_detail: str = ""
    transport_error: str | None = None


@runtime_checkable
class CodingHarness(Protocol):
    """Protocol for a coding-agent harness launched and polled in an `Environment`."""

    @property
    def name(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def icon(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def aliases(self) -> tuple[str, ...]: ...

    def new_session_id(self) -> str | None: ...

    def build_env(self, base: dict[str, str]) -> dict[str, str]: ...

    async def start(self, env: Environment, req: RunRequest) -> Any: ...

    async def poll(self, env: Environment, run: Any) -> HarnessPoll: ...

    async def cancel(self, env: Environment, run: Any) -> None: ...

    async def cleanup(self, env: Environment, thread_id: str) -> None: ...


@dataclass
class CliRun:
    handle: runs.RunHandle
    cursor: runs.RunCursor = field(default_factory=runs.RunCursor)
    parse_state: dict[str, Any] = field(default_factory=dict)


class CliHarness:
    """Base for CLI harnesses launched detached and observed via stdout JSONL."""

    name: ClassVar[str]
    display_name: ClassVar[str]
    icon: ClassVar[str]
    description: ClassVar[str]
    aliases: ClassVar[tuple[str, ...]] = ()

    def build_argv(self, req: RunRequest) -> list[str]:
        raise NotImplementedError

    def thread_files(self, req: RunRequest) -> dict[str, str]:
        return {THREAD_CONTEXT_FILE: req.system_prompt}

    def parse_line(
        self, line: str, state: dict[str, Any]
    ) -> list[HarnessEvent]:
        raise NotImplementedError

    def new_session_id(self) -> str | None:
        return None

    def build_env(self, base: dict[str, str]) -> dict[str, str]:
        return base

    async def start(self, env: Environment, req: RunRequest) -> CliRun:
        await write_thread_files(env, req.thread_dir, self.thread_files(req))
        handle = await runs.launch(
            env,
            run_id=req.run_id,
            argv=self.build_argv(req),
            cwd=req.cwd,
            env_vars={**self.build_env(req.env), THREAD_CLONE_ENV: req.cwd},
            path_prepend=req.path_prepend,
        )
        return CliRun(handle)

    async def poll(self, env: Environment, run: CliRun) -> HarnessPoll:
        polled = await runs.poll(env, run.handle, run.cursor)
        if polled.error and not polled.lost:
            return HarnessPoll(transport_error=polled.error)
        events = [
            event
            for line in polled.lines
            for event in self.parse_line(line, run.parse_state)
        ]
        exit_code = polled.exit_code
        detail = polled.stderr_tail
        if exit_code is None and polled.lost:
            exit_code = -9
            detail = (
                polled.error
                or "The harness process disappeared without reporting an exit status."
            )
        if exit_code is None and _succeeded(events):
            # Treat the run as finished at its successful result event rather than waiting
            # for MCP client teardown before process exit.
            exit_code = 0
        return HarnessPoll(
            events=events,
            exit_code=exit_code,
            output_seen=polled.bytes_read > 0,
            error_detail=detail,
        )

    async def cancel(self, env: Environment, run: CliRun) -> None:
        await runs.cancel(env, run.handle)

    async def cleanup(self, env: Environment, thread_id: str) -> None:
        del env, thread_id


def _succeeded(events: list[HarnessEvent]) -> bool:
    return any(
        e.kind is HarnessEventKind.RESULT
        and e.result is not None
        and not e.result.is_error
        for e in events
    )


def first_line(text: str, limit: int = 200) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def short_path(path: str) -> str:
    return "/".join(str(path).rstrip("/").split("/")[-2:])


async def write_thread_files(
    env: Environment, thread_dir: str, files: dict[str, str]
) -> None:
    """Write `files` owner-only (`0600`) in one command, passing contents via environment variables."""
    if not files:
        return
    if not thread_dir and not all(path.startswith("~/") for path in files):
        raise ValueError(
            "RunRequest.thread_dir is required for per-thread files"
        )
    steps, contents = ["umask 077"], {}
    for i, (path, content) in enumerate(files.items()):
        target = (
            f'"$HOME"/{shlex.quote(path[2:])}'
            if path.startswith("~/")
            else shlex.quote(f"{thread_dir}/{path}")
        )
        steps.append(
            f'mkdir -p "$(dirname {target})" && printf %s "$VOICE_AGENT_FILE_{i}" > {target}'
        )
        contents[f"VOICE_AGENT_FILE_{i}"] = content
    result = await env.exec(" && ".join(steps), env=contents, timeout_s=15.0)
    if not result.ok:
        raise RuntimeError(
            f"Could not write the thread's harness files: {result.describe_failure()}"
        )
