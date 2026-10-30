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

"""A fake gcloud for the tests of ``infra/web_proxy.sh``, and helpers that
run the script with it. The tests/infra conftest replaces ``time.sleep``
and ``time.time``, so the waits here use ``time.monotonic`` and
``threading.Event``."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "infra" / "web_proxy.sh"
SERVICE_URL = "https://my-svc-abc123-ew.a.run.app"
WEB_SA = "genmedia-web@my-proj.iam.gserviceaccount.com"
IMPERSONATE = (
    f"auth print-identity-token --impersonate-service-account={WEB_SA} "
    f"--audiences={SERVICE_URL}"
)

# Records its arguments. `info` names the SDK folder, `components install`
# adds the component there. `run services describe` gives the service URL
# (FAKE_NO_SERVICE: not found). `auth print-identity-token` gives
# sa-token-<n> when impersonating (FAKE_NO_IMPERSONATION: refused; FAKE_TOKENS:
# refused after that many), else user-token (FAKE_NO_USER_TOKEN: refused).
# `run services proxy` returns at once, or with FAKE_PROXY_LIVE listens on
# WEB_PROXY_PORT until killed, its pid in proxy.pids.
FAKE_GCLOUD = r"""#!/bin/bash
echo "$*" >> "$FAKE_DIR/calls"
impersonated() {
    local n
    [ -z "${FAKE_NO_IMPERSONATION:-}" ] || {
        echo "ERROR: Permission 'iam.serviceAccounts.getOpenIdToken' denied" >&2
        exit 1
    }
    n=$(($(cat "$FAKE_DIR/tokens" 2>/dev/null || echo 0) + 1))
    [ "$n" -le "${FAKE_TOKENS:-99}" ] || { echo "ERROR: refused" >&2; exit 1; }
    echo "$n" > "$FAKE_DIR/tokens"
    echo "sa-token-$n"
}
case "$1 $2 $3" in
info*) echo "$FAKE_DIR/sdk" ;;
components*) mkdir -p "$FAKE_DIR/sdk/bin" && touch "$FAKE_DIR/sdk/bin/cloud-run-proxy" && chmod +x "$FAKE_DIR/sdk/bin/cloud-run-proxy" ;;
"run services describe")
    [ -z "${FAKE_NO_SERVICE:-}" ] || { echo "ERROR: service not found" >&2; exit 1; }
    echo "$FAKE_URL" ;;
"run services proxy")
    echo "proxying"
    [ -n "${FAKE_PROXY_LIVE:-}" ] || exit 0
    echo $$ >> "$FAKE_DIR/proxy.pids"
    exec "$FAKE_PYTHON" -c 'import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen()
time.sleep(600)' "$WEB_PROXY_PORT" ;;
"auth print-identity-token "*)
    case "$*" in
    *--impersonate-service-account=*) impersonated ;;
    *) [ -z "${FAKE_NO_USER_TOKEN:-}" ] || { echo "ERROR: No identity token can be obtained" >&2; exit 1; }
       echo "user-token" ;;
    esac ;;
esac
"""


def make_fake(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    (tmp_path / "sdk" / "bin").mkdir(parents=True)
    return tmp_path


def with_component(fake: Path) -> None:
    binary = fake / "sdk" / "bin" / "cloud-run-proxy"
    binary.touch()
    binary.chmod(0o755)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def environment(fake: Path, **env: str) -> dict[str, str]:
    return {
        # No real SDK on PATH (it may carry cloud-run-proxy)
        "PATH": f"{fake / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(fake),
        "TMPDIR": str(fake),
        "FAKE_DIR": str(fake),
        "FAKE_URL": SERVICE_URL,
        "FAKE_PYTHON": sys.executable,
        "PROJECT_ID": "my-proj",
        "DEFAULT_REGION": "europe-west1",
        "SERVICE_NAME": "my-svc",
        "WEB_PROXY_PORT": str(free_port()),
        "WEB_PROXY_POLL": "0.1",
    } | env


def calls(fake: Path) -> list[str]:
    calls_file = fake / "calls"
    return calls_file.read_text().splitlines() if calls_file.exists() else []


def proxy_calls(fake: Path) -> list[str]:
    return [c for c in calls(fake) if c.startswith("run services proxy")]


def proxy_pids(fake: Path) -> list[int]:
    pids = fake / "proxy.pids"
    return [int(p) for p in pids.read_text().split()] if pids.exists() else []


def run(fake: Path, *args: str, **env: str):
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=environment(fake, **env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, calls(fake)


def wait_for(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        threading.Event().wait(0.05)
    return condition()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not _zombie(pid)


def _zombie(pid: int) -> bool:
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return not state or state.startswith("Z")


def stop(pids: list[int]) -> None:
    """Cleanup: kill whatever a failed test left running."""
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
