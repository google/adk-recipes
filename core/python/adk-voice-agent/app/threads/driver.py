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

"""Thread driver: runs detached harness segments in a thread's workspace and turns their output into events."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.app_utils.user_context import warm_user_context
from app.auth.github.sandbox_token import sandbox_github_token
from app.memory.store import recall_profile
from app.threads.model import (
    FINISHED_STATES,
    Directive,
    EventKind,
    MessageKind,
    PermissionRequest,
    ThreadHandle,
    ThreadState,
)
from app.threads.prompts import (
    build_message,
    system_prompt,
)
from app.threads.summaries import summarize_turn
from app.workers import policy, runs, workspace
from app.workers.environment import Environment
from app.workers.factory import invalidate_environment, resolve_environment
from app.workers.harnesses.base import (
    CodingHarness,
    HarnessEventKind,
    HarnessResult,
    RunRequest,
    first_line,
)
from app.workers.harnesses.registry import (
    get_coding_harness,
    get_harness_registry,
)
from app.workers.workspace import (
    ProjectKind,
    is_code,
    project_kind,
    repo_of,
    thread_dir,
)

if TYPE_CHECKING:
    from app.threads.registry import ThreadRegistry

logger = logging.getLogger(__name__)

# Each poll is a local file read on the computer daemon over Direct VPC egress.
POLL_INTERVAL_S = 0.5
# Grace window for a run to exit cleanly after a gateway permission request pauses the thread.
TURN_GRACE_S = 30.0
PROGRESS_MIN_INTERVAL_S = 3.0
MAX_TRANSPORT_FAILURES = 20
RETRY_DELAY_S = 10.0

_NARRATION = {
    HarnessEventKind.PROGRESS: EventKind.PROGRESS,
    HarnessEventKind.ACTIVITY: EventKind.ACTIVITY,
}

_NEW_ROUND = frozenset({MessageKind.START, MessageKind.FOLLOWUP})


class _OutcomeKind(StrEnum):
    EXITED = "exited"
    PERMISSION = "permission"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass
class _Outcome:
    kind: _OutcomeKind
    exit_code: int | None = None
    result: HarnessResult | None = None
    detail: str = ""
    permission: PermissionRequest | None = None
    session_confirmed: bool = False

    @property
    def succeeded(self) -> bool:
        return (
            self.kind is _OutcomeKind.EXITED
            and self.exit_code == 0
            and self.result is not None
            and not self.result.is_error
        )


@dataclass
class _Segment:
    harness: CodingHarness
    env: Environment
    thread_policy: policy.Policy
    ws: workspace.Workspace
    github_token: str = ""


async def publish_branch(handle: ThreadHandle) -> tuple[bool, str]:
    """Push a code thread's branch to `origin` on its owner's computer."""
    await warm_user_context()
    env = (await resolve_environment()).env
    github = await sandbox_github_token(repo_of(handle.project))
    return await workspace.push_branch(
        env, handle.project, handle.branch, github_token=github.token
    )


async def stop_orphaned_runs(thread_ids: list[str]) -> None:
    """Stop harness processes left running by threads interrupted across a server restart."""
    try:
        env = (await resolve_environment()).env
        for thread_id in thread_ids:
            await runs.cancel_thread_runs(env, thread_id)
    except Exception:
        logger.warning(
            "Could not stop runs left by interrupted threads %s",
            thread_ids,
            exc_info=True,
        )


async def drive(
    registry: ThreadRegistry, handle: ThreadHandle, directive: Directive
) -> None:
    """Run one segment until the thread reaches a gate or finishes."""
    driver = _Driver(registry, handle)
    try:
        await driver.segment(directive)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Thread %s failed", handle.thread_id)
        await driver.fail(
            "The computer hit an unexpected error while running this thread."
        )
    if (
        handle.state in FINISHED_STATES
        and project_kind(handle.project) is ProjectKind.SCRATCH
    ):
        from app.projects import keep_if_pending

        await keep_if_pending(handle.project)


class _Driver:
    def __init__(self, registry: ThreadRegistry, handle: ThreadHandle) -> None:
        self.registry = registry
        self.handle = handle
        self.seg: _Segment | None = None

    async def segment(self, directive: Directive) -> None:
        self.seg = await self._prepare(directive)
        await self._segment(self.seg, directive)

    async def fail(self, message: str) -> None:
        if self.seg is not None:
            try:
                await self._end_iteration(self.seg, failed=True)
            except Exception:
                logger.warning(
                    "Could not finish the workspace of %s",
                    self.handle.thread_id,
                    exc_info=True,
                )
        await self.registry.fail(self.handle, message)

    async def _segment(self, seg: _Segment, directive: Directive) -> None:
        h, reg = self.handle, self.registry
        await reg.transition(h, ThreadState.EXECUTING)
        if (
            directive.kind is MessageKind.PERMISSION_GRANTED
            and directive.permission is not None
        ):
            reg.grant_once(h.thread_id, directive.permission.key)
        outcome = await self._run_with_recovery(seg, directive)

        if outcome.kind is _OutcomeKind.PAUSED:
            return
        if outcome.kind is _OutcomeKind.PERMISSION:
            await self._pause_for_permission(outcome.permission)
            return
        if outcome.kind is _OutcomeKind.FAILED:
            await self.fail(outcome.detail)
            return
        if not outcome.succeeded:
            await self.fail(_failure_reason(outcome, seg.harness.display_name))
            return
        if outcome.result is None:
            raise RuntimeError("a succeeded harness run must carry a result")
        await self._finish_execute(seg, outcome.result)

    async def _prepare(self, directive: Directive) -> _Segment:
        h, reg = self.handle, self.registry
        # Preload tokens/settings off the event loop so synchronous reads below hit memory.
        await warm_user_context()
        if directive.kind in _NEW_ROUND:
            h.profile = await recall_profile(h.owner)
        harness = get_coding_harness(h.harness)
        resolved = await resolve_environment()
        env = resolved.env
        code = is_code(h.project)
        if resolved.notice:
            await reg.emit(h, EventKind.WARNING, resolved.notice)
        if h.segment == 0:
            await reg.emit(
                h,
                EventKind.PROGRESS,
                f"Preparing {harness.display_name} in {h.project}.",
            )
        github_token = ""
        if code:
            github = await sandbox_github_token(repo_of(h.project))
            github_token = github.token
            if github.notice:
                await reg.emit(h, EventKind.WARNING, github.notice)
        await workspace.ensure_home(env)
        ws = await workspace.open_workspace(
            env,
            h.project,
            h.thread_id,
            is_active=self._is_active,
            github_token=github_token,
        )
        if ws.created and project_kind(h.project) is ProjectKind.SCRATCH:
            from app.projects import project_labels

            kept = [
                p
                for p in (await project_labels(h.owner)).names
                if project_kind(p) is ProjectKind.SCRATCH
            ]
            await workspace.prune_scratch(env, keep=kept)
        if ws.created and h.sessions:
            # Harness sessions belonged to another working directory.
            h.sessions.clear()
        h.workdir = ws.path
        if h.base_sha is None:
            h.base_sha = await workspace.head_sha(env, ws.path)
        thread_policy = policy.load_policy()
        return _Segment(harness, env, thread_policy, ws, github_token)

    def _is_active(self, thread_id: str) -> bool:
        return any(
            t.thread_id == thread_id and t.state not in FINISHED_STATES
            for t in self.registry.snapshot()
        )

    async def _run_with_recovery(
        self, seg: _Segment, directive: Directive
    ) -> _Outcome:
        h, harness = self.handle, seg.harness
        outcome, resumed = await self._run(seg, directive, fresh=False)
        if (
            resumed
            and outcome.kind is _OutcomeKind.EXITED
            and outcome.exit_code != 0
            and not outcome.session_confirmed
        ):
            # Stored session could not be resumed; start a fresh one with a handoff summary.
            h.sessions.pop(harness.name, None)
            outcome, _ = await self._run(seg, directive, fresh=True)
        if (
            outcome.result is not None
            and outcome.result.retryable
            and harness.name in h.sessions
        ):
            await self.registry.emit(
                h,
                EventKind.WARNING,
                f"{harness.display_name} hit a temporary error; resuming.",
                {
                    "warning": "retry",
                    "detail": first_line(outcome.result.text, 300),
                },
            )
            await asyncio.sleep(RETRY_DELAY_S)
            outcome, _ = await self._run(seg, directive, fresh=False)
        return outcome

    async def _run(
        self, seg: _Segment, directive: Directive, *, fresh: bool
    ) -> tuple[_Outcome, bool]:
        h, harness = self.handle, seg.harness
        session_id = None if fresh else h.sessions.get(harness.name)
        resume = session_id is not None
        if session_id is None:
            session_id = harness.new_session_id()
        handoff = not resume and directive.kind is not MessageKind.START
        # May shell out to gcloud for a token: keep off the live audio event loop.
        credentials = await asyncio.to_thread(seg.env.credential_env)
        mcp_env: dict[str, str] = {}
        try:
            from app.api.mcp import mcp_env_for_thread

            mcp_env = mcp_env_for_thread(
                h.thread_id, h.owner, base_url=seg.env.mcp_base_url
            )
        except ImportError:
            pass

        h.segment += 1
        ws, home = seg.ws, seg.env.workspace_root
        from app.projects import default_name, project_labels

        project_name = (await project_labels(h.owner)).names.get(
            h.project
        ) or default_name(h.project, h.title)
        request = RunRequest(
            thread_id=h.thread_id,
            run_id=f"{h.thread_id}-{h.iteration}-{h.segment}",
            prompt=build_message(directive, h, handoff=handoff),
            system_prompt=system_prompt(
                project=h.project,
                cwd=ws.path,
                home=home,
                branch=ws.branch,
                profile=h.profile,
                project_name=project_name,
            ),
            cwd=ws.path,
            thread_dir=thread_dir(home, h.thread_id),
            session_id=session_id,
            resume=resume,
            env={**credentials, **workspace.git_env(), **mcp_env},
        )
        start = asyncio.ensure_future(harness.start(seg.env, request))
        try:
            run = await asyncio.shield(start)
        except asyncio.CancelledError:
            await asyncio.shield(
                _stop_when_started(h.thread_id, harness, seg.env, start)
            )
            raise
        try:
            await self.registry.save(h)
            return await self._watch(seg, run), resume
        except asyncio.CancelledError:
            await asyncio.shield(_stop(h.thread_id, harness, seg.env, run))
            raise
        finally:
            _revoke_mcp_token(h.thread_id)

    async def _watch(self, seg: _Segment, run: Any) -> _Outcome:
        h, reg, harness = self.handle, self.registry, seg.harness
        loop = asyncio.get_running_loop()
        last_output = loop.time()
        last_narration_at, last_narration = 0.0, ""
        transport_failures = 0
        result: HarnessResult | None = None
        confirmed = False
        paused_at: float | None = None

        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            polled = await harness.poll(seg.env, run)
            transport_error = polled.transport_error
            now = loop.time()
            if transport_error:
                transport_failures += 1
                if transport_failures >= MAX_TRANSPORT_FAILURES:
                    logger.warning(
                        "Thread %s lost contact with the environment: %s",
                        h.thread_id,
                        transport_error,
                    )
                    await _stop(h.thread_id, harness, seg.env, run)
                    invalidate_environment()
                    return _Outcome(
                        _OutcomeKind.FAILED,
                        detail="Lost contact with the user's computer.",
                    )
                continue
            transport_failures = 0

            permission: PermissionRequest | None = None
            for event in polled.events:
                if event.kind is HarnessEventKind.SESSION and event.session_id:
                    h.sessions[harness.name] = event.session_id
                    confirmed = True
                elif (
                    event.kind is HarnessEventKind.PERMISSION
                    and event.permission is not None
                ):
                    permission = event.permission
                elif event.kind is HarnessEventKind.RESULT:
                    result = event.result
                elif (
                    event.message
                    and event.message != last_narration
                    and now - last_narration_at >= PROGRESS_MIN_INTERVAL_S
                ):
                    await reg.emit(h, _NARRATION[event.kind], event.message)
                    last_narration_at = now
                    last_narration = event.message
            if polled.output_seen or polled.events:
                last_output = now

            if h.state is ThreadState.AWAITING_PERMISSION:
                # A gateway permission request paused the thread mid-run: the harness was
                # told to end its turn. No stall while it does, and its end is not a
                # failure; stop it if it keeps going.
                paused_at = now if paused_at is None else paused_at
                if polled.exit_code is None and now - paused_at < TURN_GRACE_S:
                    continue
                if polled.exit_code is None:
                    await _stop(h.thread_id, harness, seg.env, run)
                return _Outcome(
                    _OutcomeKind.PAUSED,
                    session_confirmed=confirmed,
                )
            if permission is not None:
                await _stop(h.thread_id, harness, seg.env, run)
                return _Outcome(
                    _OutcomeKind.PERMISSION,
                    permission=permission,
                    session_confirmed=confirmed,
                )
            if polled.exit_code is not None:
                return _Outcome(
                    _OutcomeKind.EXITED,
                    exit_code=polled.exit_code,
                    result=result,
                    detail=polled.error_detail,
                    session_confirmed=confirmed,
                )
            if now - last_output >= seg.thread_policy.stall_timeout_s:
                await _stop(h.thread_id, harness, seg.env, run)
                return _Outcome(
                    _OutcomeKind.FAILED,
                    detail=f"{harness.display_name} produced no output for "
                    f"{_duration(seg.thread_policy.stall_timeout_s)}, so the run was stopped.",
                )

    async def _pause_for_permission(
        self, permission: PermissionRequest | None
    ) -> None:
        if permission is None:
            raise ValueError("a permission pause needs a permission request")
        await self.registry.pause_for_permission(self.handle, permission)

    async def _finish_execute(
        self, seg: _Segment, result: HarnessResult
    ) -> None:
        h = self.handle
        if (
            h.state is ThreadState.AWAITING_PERMISSION
            and h.permission is not None
        ):
            # A gateway call paused the thread as the run ended; the approved run ends the iteration.
            await self.registry.save(h)
            return
        h.result_text = result.text
        h.summary = await summarize_turn(
            h.turns[-1].prompt if h.turns else h.goal, result.text
        )
        finish = await self._end_iteration(seg, failed=False)
        if not finish.landed:
            files = ", ".join(finish.problem_files) or "conflict"
            await self.registry.emit(
                h,
                EventKind.WARNING,
                f"This turn's work stays in {finish.workdir}: it couldn't be "
                f"merged into {h.project} ({files}).",
                {
                    "warning": "landing",
                    "files": finish.problem_files,
                    "workdir": finish.workdir,
                },
            )
        completion: dict[str, Any] = {
            "files_changed": finish.files,
            "diff_summary": finish.summary,
            "pushed": h.pushed,
            "branch": seg.ws.branch,
            "commit_sha": finish.sha,
        }
        await self.registry.complete(h, completion)

    async def _end_iteration(
        self, seg: _Segment, *, failed: bool
    ) -> workspace.Finish:
        h = self.handle
        finish = await workspace.finish_workspace(
            seg.env,
            seg.ws,
            message=f"thread {h.thread_id}: {h.title}"
            + (" (failed)" if failed else ""),
            base=h.base_sha,
        )
        h.files_changed = finish.files
        h.diff_summary = finish.summary
        h.raw_diff = finish.raw_diff
        if finish.sha and finish.sha != h.commit_sha:
            h.pushed = False
        h.commit_sha = finish.sha or h.commit_sha
        h.artifacts = finish.artifacts
        h.workdir = finish.workdir
        if finish.landed or failed:
            h.base_sha = None
            for harness in get_harness_registry().list_all():
                await harness.cleanup(seg.env, h.thread_id)
            await workspace.clear_thread_dir(seg.env, h.thread_id)
        if failed and not finish.landed:
            await self.registry.emit(
                h,
                EventKind.WARNING,
                f"This iteration's work stays in {finish.workdir}: it couldn't be "
                f"merged into {h.project} ({', '.join(finish.problem_files) or 'conflict'}).",
                {"warning": "landing", "files": finish.problem_files},
            )
        return finish


def _failure_reason(outcome: _Outcome, harness_name: str) -> str:
    if outcome.exit_code == 0 and outcome.result is None:
        return f"{harness_name} finished without reporting a result"
    result = outcome.result
    detail = (
        result.text if result is not None and result.is_error else ""
    ) or outcome.detail
    if detail:
        return first_line(detail, 300)
    return f"{harness_name} exited with code {outcome.exit_code}"


def _duration(seconds: float) -> str:
    return (
        f"{round(seconds)} seconds"
        if seconds < 90
        else f"{round(seconds / 60)} minutes"
    )


def _revoke_mcp_token(thread_id: str) -> None:
    try:
        from app.api.mcp import revoke_mcp_token
    except ImportError:
        return
    revoke_mcp_token(thread_id)


async def _stop(
    thread_id: str, harness: CodingHarness, env: Environment, run: Any
) -> None:
    # Best effort: the run may already be gone, and a failed stop must not mask the outcome.
    try:
        await harness.cancel(env, run)
    except Exception:
        logger.warning(
            "Could not stop the %s run of thread %s",
            harness.name,
            thread_id,
            exc_info=True,
        )


async def _stop_when_started(
    thread_id: str,
    harness: CodingHarness,
    env: Environment,
    start: asyncio.Future[Any],
) -> None:
    try:
        run = await start
    except Exception:
        return  # The launch failed, so there is nothing to stop.
    await _stop(thread_id, harness, env, run)
