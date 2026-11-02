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

"""Fake terraform, gcloud and sleep for the tests of ``infra/setup_infra.sh``
and the ``config.env`` target, and a runner on a pseudo-terminal."""

import os
import pty
import select
import subprocess
from pathlib import Path

# Records its arguments. FAKE_PLAN and FAKE_APPLY: the exit codes of each
# plan and apply, one per call, the last repeats. FAKE_STATE: the state.
# FAKE_IMPORT: the exit code of import.
FAKE_TERRAFORM = r"""#!/bin/bash
[ "$1" = version ] && { echo "Terraform v1.9.0"; exit 0; }
shift  # -chdir=...
echo "terraform $*" >> "$FAKE_DIR/calls"
code_for() {
    local n i=0 code
    n=$(grep -c "^terraform $1" "$FAKE_DIR/calls")
    for code in $2; do i=$((i + 1)); [ "$i" -lt "$n" ] || break; done
    echo "$code"
}
case "$1" in
state) printf '%s\n' ${FAKE_STATE:-} ;;
import) exit "${FAKE_IMPORT:-0}" ;;
plan) echo "Plan: 1 to add, 0 to change, 0 to destroy."
      exit "$(code_for plan "${FAKE_PLAN:-2}")" ;;
apply) exit "$(code_for apply "${FAKE_APPLY:-0}")" ;;
esac
"""

# Records its arguments; describe finds what FAKE_EXISTING names, and
# get-value project answers FAKE_PROJECT.
FAKE_GCLOUD = r"""#!/bin/bash
echo "gcloud $*" >> "$FAKE_DIR/calls"
case "$1 $2 $3" in
"config get-value project") echo "${FAKE_PROJECT:-}"; exit 0 ;;
"storage buckets describe") name=${4#gs://} ;;
"iam service-accounts describe") name=${4%%@*} ;;
"artifacts repositories describe") name=$4 ;;
*) exit 0 ;;
esac
case " ${FAKE_EXISTING:-} " in *" $name "*) exit 0 ;; esac
exit 1
"""

FAKE_SLEEP = '#!/bin/bash\necho "sleep $*" >> "$FAKE_DIR/calls"\n'


def make_fakes(tmp_path: Path) -> dict[str, str]:
    """The fakes in ``tmp_path/bin``; the environment that runs them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {
        "terraform": FAKE_TERRAFORM,
        "gcloud": FAKE_GCLOUD,
        "sleep": FAKE_SLEEP,
    }
    for name, text in fakes.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
    }


def calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text().splitlines() if path.exists() else []


def run(command: list[str], env: dict[str, str], cwd: Path, answer=None):
    """Run ``command``; on a pseudo-terminal that answers ``answer`` when
    it is not None, otherwise with no terminal. (exit code, output)"""
    if answer is None:
        done = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return done.returncode, done.stdout + done.stderr
    leader, follower = pty.openpty()
    os.write(leader, answer.encode())
    proc = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        stdin=follower,
        stdout=follower,
        stderr=follower,
        timeout=60,
        check=False,
    )
    out = b""  # read while the terminal is open: macOS drops it on close
    while select.select([leader], [], [], 0.5)[0]:
        out += os.read(leader, 4096)
    os.close(follower)
    os.close(leader)
    return proc.returncode, out.decode()
