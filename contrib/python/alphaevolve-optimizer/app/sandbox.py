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
"""Runs untrusted candidate programs in a separate, resource-limited process.

The candidate is written to a fresh temporary directory and executed by a
small harness in an isolated Python interpreter (`python -I -B`) with an empty
environment, a wall-clock timeout and, on POSIX, CPU / memory / file-size
limits. The harness only returns the raw outputs; scoring happens in the
parent process so a candidate cannot fake its own score.

NOTE: This is process isolation, not a security sandbox. Run the recipe in a
container (see the Dockerfile) and do not expose it to untrusted users without
a stronger sandbox such as gVisor.
"""

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

MAX_CODE_CHARS = 20_000
_MAX_ERROR_CHARS = 500
_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

_HARNESS = r"""
import importlib.util
import json
import sys

real_stdout = sys.stdout
sys.stdout = sys.stderr  # Candidate prints must not corrupt the result.
payload = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location("candidate", "candidate.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
fn = getattr(module, payload["entry_point"])
outputs = [fn(**inst["args"]) for inst in payload["instances"]]
real_stdout.write(json.dumps({"outputs": outputs}))
"""


def _limit_resources(
    cpu_seconds: int,
) -> None:  # pragma: no cover - runs in child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES)
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def run_candidate(
    code: str,
    entry_point: str,
    instances: list[dict[str, Any]],
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Executes `code` and returns its outputs for every instance.

    Returns:
        `{"ok": True, "outputs": [...]}` on success, otherwise
        `{"ok": False, "error": "..."}` with a short, model-readable reason.
    """
    if len(code) > MAX_CODE_CHARS:
        return {
            "ok": False,
            "error": f"code exceeds {MAX_CODE_CHARS} characters",
        }

    with tempfile.TemporaryDirectory(prefix="candidate-") as workdir:
        with open(
            os.path.join(workdir, "candidate.py"), "w", encoding="utf-8"
        ) as f:
            f.write(code)
        preexec = None
        if os.name == "posix":
            cpu = max(1, int(timeout_seconds))

            def preexec() -> None:  # pragma: no cover - runs in child
                _limit_resources(cpu)

        try:
            proc = subprocess.run(  # noqa: S603 -- fixed argv; isolation is the point
                [sys.executable, "-I", "-B", "-c", _HARNESS],
                input=json.dumps(
                    {"entry_point": entry_point, "instances": instances}
                ),
                capture_output=True,
                text=True,
                cwd=workdir,
                env={},
                timeout=timeout_seconds,
                preexec_fn=preexec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"timed out after {timeout_seconds:.0f}s",
            }

    if proc.returncode != 0:
        stderr = proc.stderr.strip().splitlines()
        reason = stderr[-1] if stderr else f"exit code {proc.returncode}"
        return {"ok": False, "error": reason[:_MAX_ERROR_CHARS]}
    try:
        return {"ok": True, "outputs": json.loads(proc.stdout)["outputs"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        return {
            "ok": False,
            "error": "candidate did not return JSON-serializable output",
        }
