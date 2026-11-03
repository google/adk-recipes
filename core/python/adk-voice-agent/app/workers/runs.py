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

"""Detached harness runs: launch, poll, cancel."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field

from app.workers import policy
from app.workers.environment import Environment, SandboxEnvironment

# Skip lines longer than this (e.g. huge tool results) rather than stalling the stream.
POLL_READ_CAP = 2 * 1024 * 1024
RUN_RETENTION_DAYS = 7
# Wall-clock cushion above stall budget to catch runs whose driver went away.
RUN_GRACE_S = 30 * 60

_SUPERVISOR = """\
import json, os, signal, subprocess, sys
run_dir = sys.argv[1]
with open(os.path.join(run_dir, "spec.json")) as f:
    spec = json.load(f)
limit = spec.get("max_runtime_s")
timed_out = False
with open(os.path.join(run_dir, "stdout.jsonl"), "wb") as out, open(os.path.join(run_dir, "stderr.log"), "wb") as err:
    try:
        child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    except OSError as exc:
        err.write(f"failed to start {spec['argv'][0]}: {exc}\\n".encode())
        status = {"exit_code": 127}
    else:
        try:
            status = {"exit_code": child.wait(timeout=limit)}
        except subprocess.TimeoutExpired:
            # Stop the child's process group (this supervisor's own) but outlive the TERM.
            timed_out = True
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(os.getpgrp(), signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            message = f"Stopped: the run exceeded its maximum run time of {round(limit)} seconds."
            err.write(("\\n" + message + "\\n").encode())
            status = {"exit_code": -9, "timed_out": True, "error": message}
tmp = os.path.join(run_dir, "status.tmp")
with open(tmp, "w") as f:
    json.dump(status, f)
os.replace(tmp, os.path.join(run_dir, "status.json"))
if timed_out:
    # Sweep anything the child left behind; this ends the supervisor too.
    os.killpg(os.getpgrp(), signal.SIGKILL)
"""

_LAUNCHER = """\
import os, subprocess, sys
run_dir = sys.argv[1]
p = subprocess.Popen([sys.executable, os.path.join(run_dir, "runner.py"), run_dir], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
with open(os.path.join(run_dir, "pid"), "w") as f:
    f.write(str(p.pid))
print(p.pid)
"""

_SPLIT = "__VOICE_AGENT_SPLIT__"

# A finished supervisor may linger as a zombie, which `kill -0` reports as alive.
_ALIVE_TEST = '[ -n "$P" ] && ps -o stat= -p "$P" 2>/dev/null | grep -q "^[^Z]"'


@dataclass
class RunHandle:
    run_id: str
    run_dir: str


@dataclass
class RunCursor:
    offset: int = 0
    skipping_long_line: bool = False


@dataclass
class RunPoll:
    lines: list[str] = field(default_factory=list)
    bytes_read: int = 0
    exit_code: int | None = None
    lost: bool = False
    stderr_tail: str = ""
    error: str | None = None


def default_max_runtime_s() -> float:
    budget = policy.load_policy()
    return budget.stall_timeout_s + RUN_GRACE_S


def run_dir_for(env: Environment, run_id: str) -> str:
    return f"{env.workspace_root}/.voice-agent/runs/{run_id}"


async def launch(
    env: Environment,
    *,
    run_id: str,
    argv: list[str],
    cwd: str,
    env_vars: dict[str, str] | None = None,
    path_prepend: list[str] | None = None,
    max_runtime_s: float | None = None,
) -> RunHandle:
    run_dir = run_dir_for(env, run_id)
    if max_runtime_s is None:
        max_runtime_s = default_max_runtime_s()

    if isinstance(env, SandboxEnvironment):
        data = await env.start_run(
            run_id=run_id,
            argv=argv,
            cwd=cwd,
            run_dir=run_dir,
            env_vars=env_vars,
            path_prepend=path_prepend,
            max_runtime_s=max_runtime_s,
        )
        return RunHandle(
            run_id=run_id, run_dir=str(data.get("run_dir") or run_dir)
        )

    runs_root = run_dir.rsplit("/", 1)[0]
    spec = json.dumps(
        {"argv": argv, "cwd": cwd, "max_runtime_s": max_runtime_s}
    )
    path_export = (
        f'export PATH="{":".join(path_prepend)}:$PATH" && '
        if path_prepend
        else ""
    )
    command = (
        f"mkdir -p {shlex.quote(run_dir)} && "
        f"(find {shlex.quote(runs_root)} -mindepth 1 -maxdepth 1 -mtime +{RUN_RETENTION_DAYS} "
        f"-exec rm -rf {{}} + 2>/dev/null || true) && "
        f"printf %s {shlex.quote(spec)} > {shlex.quote(run_dir)}/spec.json && "
        f"printf %s {shlex.quote(_SUPERVISOR)} > {shlex.quote(run_dir)}/runner.py && "
        f"{path_export}python3 -c {shlex.quote(_LAUNCHER)} {shlex.quote(run_dir)}"
    )
    result = await env.exec(command, env=env_vars, timeout_s=30.0)
    if not result.ok or not result.stdout.strip().isdigit():
        raise RuntimeError(
            f"Could not launch {argv[0]}: {result.describe_failure()}"
        )
    return RunHandle(run_id=run_id, run_dir=run_dir)


async def poll(
    env: Environment, handle: RunHandle, cursor: RunCursor
) -> RunPoll:
    if isinstance(env, SandboxEnvironment):
        data = await env.poll_run(
            handle.run_id,
            cursor=cursor.offset,
            skipping_long_line=cursor.skipping_long_line,
            max_bytes=POLL_READ_CAP,
        )
        if data.get("transport_error"):
            return RunPoll(error=str(data["transport_error"]))
        cursor.offset = int(data.get("cursor", cursor.offset))
        cursor.skipping_long_line = bool(data.get("skipping_long_line", False))
        exit_raw = data.get("exit_code")
        return RunPoll(
            lines=list(data.get("lines") or []),
            bytes_read=int(data.get("bytes_read") or 0),
            exit_code=int(exit_raw) if exit_raw is not None else None,
            lost=bool(data.get("lost", False)),
            stderr_tail=str(data.get("stderr_tail") or ""),
            error=data.get("error"),
        )

    d = shlex.quote(handle.run_dir)
    command = (
        f"cd {d} 2>/dev/null || {{ echo __NO_RUN__; exit 0; }}; "
        f"cat status.json 2>/dev/null; echo; echo {_SPLIT}; "
        # Liveness first: once the supervisor is gone, a missing status.json is definitive.
        f"P=$(cat pid 2>/dev/null); if ! {{ {_ALIVE_TEST}; }} && [ ! -f status.json ]; then echo lost; fi; "
        f"echo {_SPLIT}; "
        f"[ -f status.json ] && tail -c 4000 stderr.log 2>/dev/null; echo {_SPLIT}; "
        f"tail -c +{cursor.offset + 1} stdout.jsonl 2>/dev/null | head -c {POLL_READ_CAP}"
    )
    result = await env.exec(command, timeout_s=30.0)
    if result.error:
        return RunPoll(error=result.error)
    if result.stdout.startswith("__NO_RUN__"):
        return RunPoll(
            lost=True, error=f"Run directory {handle.run_dir} is missing."
        )

    parts = result.stdout.split(_SPLIT + "\n", 3)
    if len(parts) < 4:
        return RunPoll(error=f"Unexpected poll output: {result.stdout[:200]!r}")
    status_raw, lost_raw, stderr_tail, stdout_chunk = parts

    poll_result = RunPoll(
        stderr_tail=stderr_tail.strip(), lost=lost_raw.strip() == "lost"
    )
    if status_raw.strip():
        try:
            status = json.loads(status_raw)
            poll_result.exit_code = int(status["exit_code"])
        except (ValueError, KeyError, TypeError):
            poll_result.error = f"Unreadable status.json: {status_raw[:200]!r}"
        else:
            if status.get("error"):
                # The supervisor's own reason (the run timed out) leads the detail.
                poll_result.stderr_tail = (
                    f"{status['error']}\n{poll_result.stderr_tail}".strip()
                )

    raw = stdout_chunk.encode("utf-8")
    poll_result.bytes_read = len(raw)
    if cursor.skipping_long_line:
        newline = raw.find(b"\n")
        if newline < 0:
            cursor.offset += len(raw)
            return poll_result
        cursor.offset += newline + 1
        cursor.skipping_long_line = False
        raw = raw[newline + 1 :]

    end = raw.rfind(b"\n")
    if end < 0:
        if len(raw) >= POLL_READ_CAP:
            cursor.offset += len(raw)
            cursor.skipping_long_line = True
        return poll_result
    complete = raw[: end + 1]
    cursor.offset += len(complete)
    poll_result.lines = [
        ln
        for ln in complete.decode("utf-8", errors="replace").splitlines()
        if ln.strip()
    ]
    return poll_result


_TERMINATE = (
    "P=$(cat pid 2>/dev/null); "
    'if [ -n "$P" ]; then kill -TERM -"$P" 2>/dev/null || kill -TERM "$P" 2>/dev/null; '
    'sleep 1; kill -KILL -"$P" 2>/dev/null; fi; '
    '[ -f status.json ] || printf \'{"exit_code": -15, "cancelled": true}\' > status.json'
)


async def cancel(env: Environment, handle: RunHandle) -> None:
    if isinstance(env, SandboxEnvironment):
        await env.cancel_run(handle.run_id)
        return
    d = shlex.quote(handle.run_dir)
    await env.exec(
        f"cd {d} 2>/dev/null || exit 0; {_TERMINATE}", timeout_s=20.0
    )


async def cancel_thread_runs(env: Environment, thread_id: str) -> None:
    pattern = shlex.quote(run_dir_for(env, f"{thread_id}-")) + "*"
    await env.exec(
        f'for d in {pattern}; do [ -d "$d" ] && [ ! -f "$d/status.json" ] '
        f'&& (cd "$d" && {_TERMINATE}); done; true',
        timeout_s=60.0,
    )
