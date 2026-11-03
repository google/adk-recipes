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

"""Thread gates and failures: permission requests, gateway round-trips, stalls, and harness errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.api.serializers import thread_detail
from app.threads import driver
from app.threads.model import (
    PermissionKind,
    PermissionRequest,
    ThreadHandle,
    ThreadState,
)
from app.threads.registry import ThreadError
from app.workers.harnesses.registry import get_coding_harness
from tests.fakes import GOAL, REPO, Lab, git, kinds, wait_until

pytestmark = pytest.mark.asyncio

SHARE = PermissionRequest(
    action_class="google_drive.share*",
    command='google_drive__share_file {"id":"doc-1"}',
    key="share-key-0001",
    kind=PermissionKind.TOOL,
)
SEND = PermissionRequest(
    action_class="gmail.send*",
    command='gmail__send_message {"to":"team@example.com"}',
    key="send-key-0001",
    kind=PermissionKind.TOOL,
)
HANGING_TURN = {"steps": [{"say": "Working on it"}, {"hang": True}]}


@pytest_asyncio.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    from app.fast_api_app import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def run_requests(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every `RunRequest` the driver builds."""
    seen: list[Any] = []
    real = driver.RunRequest

    def spy(**kwargs: Any) -> Any:
        request = real(**kwargs)
        seen.append(request)
        return request

    monkeypatch.setattr(driver, "RunRequest", spy)
    return seen


async def test_blocked_command_waits_for_permission_and_runs_once_granted(
    lab: Lab,
) -> None:
    lab.agent.script(
        {"steps": [{"run": "git push origin main"}]},
        {"steps": [{"run": "git push origin main"}]},
    )
    handle = await lab.dispatch()

    assert handle.state is ThreadState.AWAITING_PERMISSION
    assert handle.permission is not None
    assert handle.permission.action_class == "merge"
    assert handle.permission.command == "git push origin main"
    assert kinds(handle).count("permission_requested") == 1
    assert lab.agent.commands[0]["exit_code"] == 126

    await lab.registry.approve(handle.thread_id)
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert handle.permission is None
    granted = lab.agent.calls[1]
    assert "Approved: `git push origin main`" in granted["prompt"]
    assert lab.agent.commands[1]["exit_code"] not in (0, 126), (
        "the grant let real git run (no remote here)"
    )


async def test_an_approval_lost_to_a_transient_error_is_resent(
    lab: Lab,
) -> None:
    push = {"steps": [{"run": "git push origin main"}]}
    lab.agent.script(push, {"interrupted": True}, push)
    handle = await lab.dispatch(harness="antigravity")
    assert handle.state is ThreadState.AWAITING_PERMISSION

    await lab.registry.approve(handle.thread_id)
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    lost, resent = lab.agent.calls[1:]
    assert resent["prompt"] == lost["prompt"]
    assert "Approved: `git push origin main`" in resent["prompt"]
    assert lab.agent.commands[1]["exit_code"] not in (0, 126)


async def test_denied_permission_continues_without_the_command(
    lab: Lab,
) -> None:
    lab.agent.script({"steps": [{"run": "git reset --hard HEAD~1"}]}, {})
    handle = await lab.dispatch()
    assert handle.permission is not None
    assert handle.permission.action_class == "destructive"

    await lab.registry.steer(handle.thread_id, "Keep the history")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED
    assert (
        "denied `git reset --hard 'HEAD~1'` (Keep the history)"
        in lab.agent.calls[1]["prompt"]
    )
    assert len(lab.agent.commands) == 1


async def test_a_code_thread_pushes_its_branch_only_when_published(
    lab: Lab, tmp_path: Path
) -> None:
    seed, remote = tmp_path / "seed", tmp_path / "remote.git"
    who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    git(str(tmp_path), "init", "-q", "-b", "main", str(seed))
    git(str(seed), *who, "commit", "-q", "--allow-empty", "-m", "seed")
    git(str(tmp_path), "clone", "-q", "--bare", str(seed), str(remote))
    base = f"{lab.env.workspace_root}/code/{REPO}"
    git(str(tmp_path), "clone", "-q", str(remote), base)
    lab.agent.script({"steps": [{"write": {"greet.py": "x = 1\n"}}]})

    handle = await lab.dispatch()
    assert handle.workdir is not None
    assert handle.branch is not None
    assert git(handle.workdir, "remote", "get-url", "origin").strip() == str(
        remote
    )
    assert handle.state is ThreadState.COMPLETED and not handle.pushed, (
        handle.error
    )
    assert git(str(remote), "branch", "--list", handle.branch) == "", (
        "finishing a round pushes nothing"
    )

    await lab.registry.publish(handle.thread_id)
    assert handle.pushed
    assert (
        git(str(remote), "rev-parse", handle.branch).strip()
        == handle.commit_sha
    )
    assert git(base, "status", "--porcelain") == ""


async def test_silent_harness_is_stopped_as_stalled(lab: Lab) -> None:
    lab.set_policy(stall_timeout_min=0.02)
    lab.agent.script({"steps": [{"hang": True}]})
    handle = await lab.dispatch()
    assert handle.state is ThreadState.FAILED
    assert handle.error is not None and "produced no output" in handle.error
    assert kinds(handle)[-1] == "error"


@pytest.mark.parametrize(
    ("turn", "message"),
    [
        (
            {"is_error": True, "result": "Model quota exhausted"},
            "Model quota exhausted",
        ),
        ({"no_result": True}, "finished without reporting a result"),
        ({"no_result": True, "exit_code": 3}, "exited with code 3"),
    ],
)
async def test_harness_failures_end_the_thread(
    lab: Lab, turn: dict[str, Any], message: str
) -> None:
    lab.agent.script(turn)
    handle = await lab.dispatch()
    assert handle.state is ThreadState.FAILED
    assert handle.error is not None and message in handle.error


async def test_unexpected_environment_error_is_sanitized(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_resolve():
        raise RuntimeError("HTTP 500: http://internal/secret body-detail")

    monkeypatch.setattr(driver, "resolve_environment", broken_resolve)
    handle = await lab.dispatch()
    assert handle.state is ThreadState.FAILED
    assert handle.error == (
        "The computer hit an unexpected error while running this thread."
    )
    assert "http://internal/secret" not in handle.error
    assert "body-detail" not in handle.error


async def test_an_interrupted_stream_resumes_once(lab: Lab) -> None:
    lab.agent.script(
        {"interrupted": True, "steps": [{"write": {"greet.py": "x = 1\n"}}]},
        {},
    )
    handle = await lab.dispatch(harness="antigravity")
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert handle.files_changed == ["greet.py"]
    execute, resumed = lab.agent.calls
    assert resumed["resume"] == execute["session"]
    assert resumed["prompt"] == execute["prompt"]


# ---- Gateway approval round-trip ----------------------------------------------


async def _paused_mid_run(
    lab: Lab, permission: PermissionRequest
) -> ThreadHandle:
    handle = await lab.registry.dispatch(GOAL, REPO)
    await lab.wait_for_calls(1)
    await wait_until(
        lambda: handle.state is ThreadState.EXECUTING, message="executing"
    )
    assert (
        await lab.registry.request_permission(handle.thread_id, permission)
        is False
    )
    assert handle.state is ThreadState.AWAITING_PERMISSION
    return handle


async def test_a_bridge_pause_suspends_stall_detection_and_stops_the_run(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, run_requests: list[Any]
) -> None:
    monkeypatch.setattr(driver, "TURN_GRACE_S", 2.0)
    lab.agent.script(HANGING_TURN, {})
    harness = get_coding_harness("claude")
    start = harness.start

    async def start_then_ask(env: Any, request: Any) -> Any:
        run = await start(env, request)
        monkeypatch.setattr(harness, "start", start)
        await lab.wait_for_calls(1)
        assert (
            await lab.registry.request_permission(request.thread_id, SHARE)
            is False
        )
        return run

    monkeypatch.setattr(harness, "start", start_then_ask)
    lab.set_policy(stall_timeout_min=0.01)
    handle = await lab.dispatch(GOAL, REPO)

    assert handle.state is ThreadState.AWAITING_PERMISSION, handle.error
    assert "error" not in kinds(handle)
    assert kinds(handle).count("permission_requested") == 1
    assert handle.permission == SHARE
    assert lab.exit_code(run_requests[0].run_id) != 0

    lab.set_policy()
    await lab.registry.approve(handle.thread_id, permission_key=SHARE.key)
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    resumed = run_requests[-1]
    assert resumed.resume and resumed.session_id == run_requests[0].session_id
    assert "google_drive__share_file" in lab.agent.calls[1]["prompt"]


async def test_an_approved_gateway_call_is_granted_once(
    lab: Lab, run_requests: list[Any]
) -> None:
    lab.agent.script(HANGING_TURN, {})
    handle = await _paused_mid_run(lab, SEND)

    await lab.registry.approve(handle.thread_id, permission_key=SEND.key)
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert len(lab.agent.calls) == 2

    assert await lab.registry.request_permission(handle.thread_id, SEND) is True
    assert (
        await lab.registry.request_permission(handle.thread_id, SEND) is False
    )
    assert handle.state is ThreadState.COMPLETED


async def test_a_denied_bridge_request_resumes_telling_the_agent(
    lab: Lab, run_requests: list[Any]
) -> None:
    lab.agent.script(HANGING_TURN, {})
    handle = await _paused_mid_run(lab, SHARE)

    await lab.registry.steer(handle.thread_id, "Send me the link instead")
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error
    assert "(Send me the link instead)" in lab.agent.calls[1]["prompt"]
    assert (
        await lab.registry.request_permission(handle.thread_id, SHARE) is False
    )


async def test_a_request_from_a_thread_that_is_not_running_is_refused(
    lab: Lab,
) -> None:
    lab.agent.script({})
    handle = await lab.dispatch()
    assert handle.state is ThreadState.COMPLETED

    assert (
        await lab.registry.request_permission(handle.thread_id, SHARE) is False
    )
    assert handle.state is ThreadState.COMPLETED
    assert "permission_requested" not in kinds(handle)


# ---- Approval key binding & two-turn voice consent ----------------------------


async def _blocked(lab: Lab) -> ThreadHandle:
    push = {"steps": [{"run": "git push origin main"}]}
    lab.agent.script(push, push)
    handle = await lab.dispatch()
    assert handle.state is ThreadState.AWAITING_PERMISSION
    return handle


async def test_an_approval_for_a_request_that_changed_is_refused(
    lab: Lab, api: httpx.AsyncClient
) -> None:
    handle = await _blocked(lab)
    assert handle.permission is not None
    key = handle.permission.key

    stale = await api.post(
        f"/api/v1/threads/{handle.thread_id}/approve",
        json={"permission_key": "an-older-request"},
    )
    assert stale.status_code == 409
    assert "review it again" in stale.json()["detail"]
    assert handle.state is ThreadState.AWAITING_PERMISSION
    assert handle.permission is not None and handle.permission.key == key

    ok = await api.post(
        f"/api/v1/threads/{handle.thread_id}/approve",
        json={"permission_key": key},
    )
    assert ok.status_code == 200, ok.text
    handle = await lab.settle(handle.thread_id)
    assert handle.state is ThreadState.COMPLETED, handle.error


async def test_thread_detail_carries_the_key_of_the_pending_permission(
    lab: Lab,
) -> None:
    handle = await _blocked(lab)
    assert handle.permission is not None
    detail = thread_detail(handle)
    assert detail["permission"] is not None
    assert detail["permission"]["key"] == handle.permission.key


def _permission_gate(handle: ThreadHandle) -> None:
    handle.permission = PermissionRequest(
        action_class="git_push",
        command="git push origin agent/thread-1",
        key="push-key",
    )
    handle.state = ThreadState.AWAITING_PERMISSION


async def test_approved_permissions_close_their_gate_in_the_store_before_resuming(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab.agent.script({})
    handle = await lab.dispatch()
    _permission_gate(handle)
    await lab.registry.save(handle)
    stored_during_resume: list[Any] = []
    real_resume = lab.registry._resume

    async def spy_resume(h: Any, directive: Any) -> None:
        loaded = await lab.store.load_thread(h.thread_id)
        assert loaded is not None
        stored_during_resume.append(loaded[0].get("permission"))
        await real_resume(h, directive)

    monkeypatch.setattr(lab.registry, "_resume", spy_resume)
    await lab.registry.approve(handle.thread_id, permission_key="push-key")

    assert stored_during_resume == [None]


async def test_a_gate_left_without_its_request_cannot_be_approved_again(
    lab: Lab,
) -> None:
    lab.agent.script({})
    handle = await lab.dispatch()
    _permission_gate(handle)
    handle.permission = None
    with pytest.raises(ThreadError, match="no request open"):
        await lab.registry.approve(handle.thread_id)
    assert handle.state is ThreadState.AWAITING_PERMISSION
