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

"""``infra/web_proxy.sh``'s identity token with a fake gcloud: genmedia-web's
for the service URL, the caller's own as a fallback, a clear error when
neither comes, a fresh one every WEB_PROXY_REFRESH seconds, and the proxy
stopped by a signal or the end of the terminal session."""

import os
import pty
import re
import select
import signal
import subprocess
import time

import pytest

from tests.infra.web_proxy_fakes import (
    IMPERSONATE,
    SCRIPT,
    WEB_SA,
    alive,
    calls,
    environment,
    make_fake,
    proxy_calls,
    proxy_pids,
    run,
    stop,
    wait_for,
    with_component,
)


@pytest.fixture
def fake(tmp_path):
    fake = make_fake(tmp_path)
    with_component(fake)
    yield fake
    stop(proxy_pids(fake))


def test_the_proxy_gets_a_token_of_genmedia_web(fake):
    result, made = run(fake)

    assert result.returncode == 0, result.stderr
    assert IMPERSONATE in made
    assert proxy_calls(fake)[-1].endswith("--quiet --token=sa-token-1")
    assert "own identity" not in result.stderr


def test_without_impersonation_it_uses_the_callers_own_token(fake):
    """A teammate without Token Creator, on a laptop."""
    result, made = run(fake, FAKE_NO_IMPERSONATION="1")

    assert result.returncode == 0, result.stderr
    assert made[-2] == "auth print-identity-token"
    assert proxy_calls(fake)[-1].endswith("--quiet --token=user-token")
    assert f"Could not act as {WEB_SA}" in result.stderr


def test_without_a_service_url_it_uses_the_callers_own_token(fake):
    result, made = run(fake, FAKE_NO_SERVICE="1")

    assert result.returncode == 0, result.stderr
    assert not [c for c in made if "--impersonate" in c]
    assert proxy_calls(fake)[-1].endswith("--token=user-token")


def test_no_token_at_all_is_a_clear_error(fake):
    """Cloud Shell without Token Creator: neither token comes."""
    result, _ = run(fake, FAKE_NO_IMPERSONATION="1", FAKE_NO_USER_TOKEN="1")

    assert result.returncode == 1
    assert not proxy_calls(fake)
    # gcloud's own errors, then what to do
    assert "iam.serviceAccounts.getOpenIdToken" in result.stderr
    assert "No identity token can be obtained" in result.stderr
    assert "Error: no identity token for the proxy" in result.stderr
    assert f"roles/iam.serviceAccountTokenCreator on {WEB_SA}" in result.stderr
    assert "make share" in result.stderr
    assert "gcloud auth login" in result.stderr


def _foreground(fake, **env):
    return subprocess.Popen(
        ["bash", str(SCRIPT)],
        env=environment(fake, FAKE_PROXY_LIVE="1", **env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stopped(fake) -> bool:
    return not any(alive(pid) for pid in proxy_pids(fake))


def test_the_proxy_restarts_with_a_fresh_token(fake):
    script = _foreground(fake, WEB_PROXY_REFRESH="1")
    try:
        assert wait_for(lambda: len(proxy_calls(fake)) >= 2)
        assert wait_for(lambda: len(proxy_pids(fake)) >= 2)
    finally:
        script.send_signal(signal.SIGTERM)
        out = script.communicate(timeout=15)[0]

    tokens = [c.rsplit("--token=", 1)[1] for c in proxy_calls(fake)]
    assert tokens[:2] == ["sa-token-1", "sa-token-2"]
    assert "Restarting the proxy with a fresh identity token" in out
    first = proxy_pids(fake)[0]
    assert not alive(first)  # stopped before the restart
    assert wait_for(lambda: _stopped(fake), 5)  # and the new one by SIGTERM


def test_a_failed_refresh_keeps_the_running_proxy(fake):
    script = _foreground(
        fake,
        WEB_PROXY_REFRESH="1",
        WEB_PROXY_RETRY="1",
        FAKE_TOKENS="1",
        FAKE_NO_USER_TOKEN="1",
    )
    try:
        assert wait_for(lambda: calls(fake).count(IMPERSONATE) >= 3)
        assert script.poll() is None
        assert len(proxy_calls(fake)) == 1
        assert alive(proxy_pids(fake)[0])
    finally:
        script.send_signal(signal.SIGTERM)
        script.communicate(timeout=15)
    assert wait_for(lambda: _stopped(fake), 5)


def test_ctrl_c_stops_make_web_and_the_proxy(fake):
    script = _foreground(fake)
    try:
        assert wait_for(lambda: proxy_pids(fake))
    finally:
        script.send_signal(signal.SIGINT)
        script.communicate(timeout=15)

    assert script.returncode == 0
    assert wait_for(lambda: _stopped(fake), 5)


# ------------------------------------------------------------ background


def _read_until(fd: int, pattern: str, timeout: float = 20.0) -> str:
    out = b""
    deadline = time.monotonic() + timeout
    while not re.search(pattern.encode(), out) and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                out += os.read(fd, 4096)
            except OSError:
                break
    return out.decode(errors="replace")


@pytest.fixture
def background(fake, request):
    """The background start on a terminal, from a session leader that stays
    (the terminal's shell): its output, the watcher's pid and the leader.
    Indirect parameter: the leader's commands before the start."""
    leader_fd, terminal_fd = pty.openpty()
    before = getattr(request, "param", "")
    command = f'{before}bash "$0" --background; exec sleep 60'
    leader = subprocess.Popen(
        ["bash", "-c", command, str(SCRIPT)],
        env=environment(fake, FAKE_PROXY_LIVE="1"),
        stdin=terminal_fd,
        stdout=terminal_fd,
        stderr=terminal_fd,
        start_new_session=True,
    )
    os.close(terminal_fd)
    out = _read_until(leader_fd, r"Log: \S+\r?\n")
    match = re.search(r"stop it with: kill (\d+)", out)
    yield out, int(match.group(1)) if match else 0, leader
    leader.kill()
    leader.wait()
    os.close(leader_fd)


def test_kill_pid_stops_the_watcher_and_the_proxy(fake, background):
    out, watcher, _ = background
    assert watcher, out
    assert proxy_calls(fake)[-1].endswith("--token=sa-token-1")
    assert proxy_pids(fake) and alive(proxy_pids(fake)[0])

    os.kill(watcher, signal.SIGTERM)

    assert wait_for(lambda: not alive(watcher), 5)
    assert wait_for(lambda: _stopped(fake), 5)


# SIGHUP ignored: the session leader's end alone, as when no hangup comes
@pytest.mark.parametrize("background", ['trap "" HUP; '], indirect=True)
def test_the_proxy_stops_when_the_terminal_session_ends(fake, background):
    out, watcher, leader = background
    assert watcher, out

    leader.kill()
    leader.wait()  # no zombie: the session leader is gone

    assert wait_for(lambda: _stopped(fake), 10)
    assert wait_for(lambda: not alive(watcher), 5)
    log = re.search(r"Log: (\S+)", out).group(1)
    assert "The terminal session ended: proxy stopped." in open(log).read()
