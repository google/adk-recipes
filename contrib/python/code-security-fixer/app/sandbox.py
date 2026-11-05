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
"""Runs the target's tools (scanner, tests) in a resource-limited process.

Every command runs with a fixed argv (no shell), a minimal environment, a
wall-clock timeout and, on POSIX, CPU / memory / file-size limits, inside a
throwaway copy of the target.

NOTE: This is process isolation, not a security sandbox. The target's tests
are arbitrary code. Run the recipe in a container (see the Dockerfile) and use
a stronger sandbox such as gVisor before pointing it at untrusted code.
"""

import os
import subprocess
import sys
from typing import Any

_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_FILE_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
_MAX_STDOUT_CHARS = 10_000_000
_MAX_STDERR_CHARS = 20_000


def _limit_resources(cpu_seconds: int) -> None:  # pragma: no cover - child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES)
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (_FILE_SIZE_LIMIT_BYTES, _FILE_SIZE_LIMIT_BYTES)
    )


def _child_env(cwd: str) -> dict[str, str]:
    """A minimal environment: no credentials or API keys leak to the target."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": cwd,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def python_argv(*args: str) -> list[str]:
    """Builds an argv that runs this interpreter in isolated mode."""
    return [sys.executable, "-I", "-B", *args]


def run_limited(
    argv: list[str], cwd: str, timeout_seconds: float
) -> dict[str, Any]:
    """Runs `argv` in `cwd` and returns its exit code and (truncated) output.

    Returns:
        `{"returncode": int | None, "stdout": str, "stderr": str,
        "timed_out": bool}`.
    """
    preexec = None
    if os.name == "posix":
        cpu = max(1, int(timeout_seconds))

        def preexec() -> None:  # pragma: no cover - runs in child
            _limit_resources(cpu)

    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell; isolation is the point
            argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_child_env(cwd),
            timeout=timeout_seconds,
            preexec_fn=preexec,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": _text(exc.stdout)[-_MAX_STDOUT_CHARS:],
            "stderr": _text(exc.stderr)[-_MAX_STDERR_CHARS:],
            "timed_out": True,
        }
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-_MAX_STDOUT_CHARS:],
        "stderr": proc.stderr[-_MAX_STDERR_CHARS:],
        "timed_out": False,
    }
