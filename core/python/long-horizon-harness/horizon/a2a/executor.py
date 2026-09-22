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

"""A2A executor: per-session task tracking + streamed-text dedup."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections import defaultdict

from a2a.server.agent_execution import RequestContext
from google.adk.a2a import _compat as a2a_compat
from google.adk.a2a.converters import part_converter
from google.adk.a2a.executor.a2a_agent_executor import (
    A2aAgentExecutor,
    A2aAgentExecutorConfig,
)
from google.adk.a2a.executor.interceptors.include_artifacts_in_a2a_event import (
    include_artifacts_in_a2a_event_interceptor,
)
from google.adk.agents.invocation_context import new_invocation_context_id
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.runners import Runner

from horizon.a2a.user_converter import request_converter
from horizon.tools.names import ARTIFACT, CLARIFY

logger = logging.getLogger(__name__)


def _is_text_part(part) -> bool:
    return a2a_compat.is_text_part(part)


# Key + values ADK stamps on function-call/response DataPart metadata
# (google.adk.a2a.converters: _get_adk_metadata_key("type")). Mirrored here and
# in web/lib/horizon-events.ts so both ends read the same wire contract.
_ADK_TYPE_KEY = "adk_type"
_TOOL_DATA_TYPES = {"function_call", "function_response"}

# Marks a text part the web should skip because it renders the same thing
# richly: the 📎 saved-artifact link (the web shows the artifact inline from its
# FilePart). Mirrored in web/lib/horizon-events.ts.
_GE_LABEL_KEY = "lha_ge_label"

# GE's thinking timeline drops any tool call whose name isn't <agent>__<action>
# (its formatToolName returns null → no chip). Our tools have flat names, so we
# prefix them when surfacing; the web strips it back in mapPart. Framework HITL
# parts are interaction prompts (not chips) the web matches by exact name, so
# they keep their flat names. Mirrored in web/lib/horizon-events.ts.
_GE_TOOL_NAME_PREFIX = "horizon__"
# "adk_request_confirmation" is an ADK framework literal, not a horizon tool.
_FRAMEWORK_TOOL_NAMES = {CLARIFY, "adk_request_confirmation"}

# The model invents [text](attachment://name) / [text](sandbox:/name) links for
# saved artifacts (the real signed URL is redacted from its view). No client
# renders these schemes, so they show as dead links — most visibly in GE. The
# real link reaches clients via the FilePart + the 📎 status, so drop the dead
# href and keep the link text. The prompt no longer tells the model to write a
# link, so this is a backstop.
_FAKE_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:attachment|sandbox):[^)\s]*\)")


def _tool_data(part):
    """The function_call/response data dict if `part` is a tool DataPart, else None.

    ``data_part_dict`` returns a COPY (proto→dict), so callers that mutate it
    (e.g. ``_ge_prefix_tool_name``) must rebuild the part via ``make_data_part``.
    """
    if not a2a_compat.is_data_part(part):
        return None
    meta = a2a_compat.part_metadata(part)
    if meta.get(_ADK_TYPE_KEY) in _TOOL_DATA_TYPES:
        return a2a_compat.data_part_dict(part)
    return None


def _ge_prefix_tool_name(data: dict) -> None:
    """Prefix the tool name in-place so GE's <agent>__<action> filter shows it."""
    name = data.get("name")
    if (
        isinstance(name, str)
        and name not in _FRAMEWORK_TOOL_NAMES
        and not name.startswith(_GE_TOOL_NAME_PREFIX)
    ):
        data["name"] = f"{_GE_TOOL_NAME_PREFIX}{name}"


def _surface_tool_calls(a2a_events: list, task_id, context_id) -> list:
    """Move function-call/response DataParts from artifact-updates onto working
    status messages.

    ADK's new-impl converter buries tool calls in artifact-updates, which A2A
    defines as data *output*, so clients never show them as activity. A2A's
    vehicle for progress is a status-update message, so re-emit each tool DataPart
    on one. The web reads the structured part (`adk_type`) to render the tool
    chip; duplicate calls (the model emits one in both its partial and final
    event) are deduped client-side by callId in chat-segments.ts.
    """
    out = []
    for ev in a2a_events:
        art = getattr(ev, "artifact", None)
        if art is None:
            out.append(ev)
            continue
        tool_parts = [p for p in (art.parts or []) if _tool_data(p)]
        if not tool_parts:
            out.append(ev)
            continue
        for p in tool_parts:
            data = _tool_data(p)
            _ge_prefix_tool_name(data)
            # data_part_dict is a copy, so rebuild the part to carry the prefix.
            part = a2a_compat.make_data_part(
                data=data, metadata=a2a_compat.part_metadata(p)
            )
            out.append(
                a2a_compat.make_task_status_update_event(
                    task_id,
                    context_id,
                    a2a_compat.make_task_status(
                        a2a_compat.TS_WORKING,
                        message=a2a_compat.make_message(
                            message_id=str(uuid.uuid4()),
                            role=a2a_compat.ROLE_AGENT,
                            parts=[part],
                        ),
                    ),
                    final=False,
                )
            )
        kept = [p for p in (art.parts or []) if not _tool_data(p)]
        if kept:
            _replace_parts(art, kept)
            out.append(ev)
    return out


def _surface_artifact_links(a2a_events: list, task_id, context_id) -> list:
    """Lift a saved artifact's link out of its function_response into a text
    status message.

    GE files data parts away and never shows them, and the model can't reliably
    echo a long signed URL, so the backend surfaces the link as text. Runs
    before _surface_tool_calls, while the response is still on the artifact part.
    The link is tagged _GE_LABEL_KEY: the web renders the artifact inline from
    its FilePart, so it skips this to avoid showing the file twice; GE keeps it.
    """
    out = []
    for ev in a2a_events:
        out.append(ev)
        art = getattr(ev, "artifact", None)
        if art is None:
            continue
        for p in art.parts or []:
            data = _tool_data(p)
            if not data or data.get("name") != ARTIFACT:
                continue
            resp = data.get("response") or {}
            url = resp.get("url")
            if not url:
                continue
            name = resp.get("filename") or "artifact"
            # GE concatenates text parts with no separator; \n\n keeps the link
            # on its own line.
            link_part = a2a_compat.make_text_part(f"\n\n📎 [{name}]({url})\n\n")
            a2a_compat.set_part_metadata(link_part, {_GE_LABEL_KEY: True})
            out.append(
                a2a_compat.make_task_status_update_event(
                    task_id,
                    context_id,
                    a2a_compat.make_task_status(
                        a2a_compat.TS_WORKING,
                        message=a2a_compat.make_message(
                            message_id=str(uuid.uuid4()),
                            role=a2a_compat.ROLE_AGENT,
                            parts=[link_part],
                        ),
                    ),
                    final=False,
                )
            )
    return out


def _strip_streamed_text(a2a_events: list) -> list:
    """Remove the redundant full-text from a streamed run's final aggregate.

    ADK closes a streamed run with one artifact-update that re-sends the entire
    text (the deltas already carried it) plus any function-call part. Clients that
    honor A2A append/last_chunk replace harmlessly; ones that naively concatenate
    every artifact part (Gemini Enterprise) render the whole reply twice. So drop
    the text parts, keep the rest, and append (not replace) so honoring clients
    don't lose the streamed text.
    """
    out = []
    for ev in a2a_events:
        art = getattr(ev, "artifact", None)
        if art is None:
            out.append(ev)
            continue
        kept = [p for p in (art.parts or []) if not _is_text_part(p)]
        if not kept:
            continue
        _replace_parts(art, kept)
        ev.append = True
        out.append(ev)
    return out


def _replace_parts(container, parts: list) -> None:
    """Replace ``container.parts`` in place — proto repeated fields reject direct
    assignment (``container.parts = [...]``), so clear + extend (works on both a
    proto repeated field and a pydantic list)."""
    del container.parts[:]
    container.parts.extend(parts)


def _strip_fake_artifact_links(a2a_events: list) -> list:
    """Replace fabricated attachment:/sandbox: artifact links with their text.

    Shape-agnostic: proto parts expose ``.text`` directly (empty string — falsy —
    for non-text parts) and accept scalar assignment; pydantic v0.3 parts expose
    ``.root.text``.
    """
    for ev in a2a_events:
        art = getattr(ev, "artifact", None)
        if art is not None:
            parts = art.parts or []
        else:
            msg = getattr(getattr(ev, "status", None), "message", None)
            parts = (msg.parts if msg else None) or []
        for p in parts:
            root = getattr(p, "root", p)
            text = getattr(root, "text", None)
            if text and ("attachment:" in text or "sandbox:" in text):
                root.text = _FAKE_LINK_RE.sub(r"\1", text)
    return a2a_events


# Side-channel for the web UI to enumerate per-context A2A tasks and detect
# in-flight ones. A2A's TaskStore has no list-by-context, so we shadow the
# mapping in session state: an append-only ordered list of task IDs, plus the
# id of the currently running task (None when nothing is in-flight).
LHA_TASK_IDS_KEY = "lha:task_ids"
LHA_ACTIVE_TASK_KEY = "lha:active_task_id"


def build_executor(*, runner: Runner) -> A2aAgentExecutor:
    """A2A executor that records per-session task lifecycle in session state."""
    return _TaskTrackingExecutor(runner=runner)


class _StreamDedupConverter:
    """The single A2A event converter — both GE and the web take this path
    (forced via ``force_new_version=True``). It drops the redundant full-text
    aggregate a streamed run emits after its deltas (`_strip_streamed_text`) and
    lifts function calls/responses out of artifact-updates onto status messages
    so they show (`_surface_tool_calls`). ADK's new-impl executor calls
    ``adk_event_converter(event, agents_artifacts, task_id, context_id, part_converter)``
    — the 2nd arg is the artifact-id state map, not an invocation context. Naive
    clients (GE) ignore A2A ``append``/``last_chunk`` and would otherwise double
    the reply; the web honors the stripped/append output and reads the function
    call/response DataParts directly."""

    def __init__(self) -> None:
        # Per-task set of artifact_ids the wire has seen created (append=False),
        # so an orphaned append=True can be demoted to a create (a2a 1.x rejects
        # append to an uncreated artifact). Freed when each stream closes.
        self._created_artifacts: dict = {}

    def __call__(
        self,
        event,
        agents_artifacts,
        task_id=None,
        context_id=None,
        part_converter_func=part_converter.convert_genai_part_to_a2a_part,
    ):
        from google.adk.a2a.converters.from_adk_event import (
            convert_event_to_a2a_events,
        )

        # A non-partial event whose author already has a tracked artifact id
        # closes a streamed run: the final aggregate re-sends the whole text the
        # deltas already carried, so strip it (see _strip_streamed_text).
        closes_stream = (
            not getattr(event, "partial", False)
            and getattr(event, "author", None) in agents_artifacts
        )
        a2a_events = convert_event_to_a2a_events(
            event, agents_artifacts, task_id, context_id, part_converter_func
        )
        if closes_stream:
            a2a_events = _strip_streamed_text(a2a_events)
        a2a_events = _surface_artifact_links(a2a_events, task_id, context_id)
        a2a_events = _surface_tool_calls(a2a_events, task_id, context_id)
        a2a_events = _strip_fake_artifact_links(a2a_events)
        out = self._enforce_artifact_lifecycle(a2a_events, task_id)
        if closes_stream:
            self._created_artifacts.pop(task_id, None)
        return out

    def _enforce_artifact_lifecycle(self, a2a_events: list, task_id) -> list:
        """Never emit ``append=True`` for an artifact the server hasn't seen created.

        a2a 1.x rejects an ``append=True`` artifact-update whose ``artifact_id`` was
        never created (``append=False``) first. Our tool/link surfacing can drop an
        artifact's create event (moving its only part onto a status message),
        orphaning a later streamed-text append. Demote such an append to a create.
        State is per-task and freed when the stream closes.
        """
        created = self._created_artifacts.setdefault(task_id, set())
        for ev in a2a_events:
            art = getattr(ev, "artifact", None)
            if art is None:
                continue
            art_id = art.artifact_id
            if getattr(ev, "append", False) and art_id not in created:
                ev.append = False
            created.add(art_id)
        return a2a_events


class _TaskTrackingExecutor(A2aAgentExecutor):
    def __init__(self, *, runner: Runner) -> None:
        super().__init__(
            runner=runner,
            force_new_version=True,
            config=A2aAgentExecutorConfig(
                adk_event_converter=_StreamDedupConverter(),
                request_converter=request_converter,
                execute_interceptors=[
                    include_artifacts_in_a2a_event_interceptor
                ],
            ),
        )
        self._session_locks: dict[tuple[str, str, str], asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )

    async def execute(self, context: RequestContext, event_queue):
        runner = await self._resolve_runner()
        run_request = self._config.request_converter(
            context, self._config.a2a_part_converter
        )
        task_id = context.task_id
        await self._mark_task_active(
            runner, run_request.user_id, run_request.session_id, task_id
        )
        cancelled = False
        try:
            await super().execute(context, event_queue)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Shield so the done-marking state delta still lands if the producer
            # was cancelled (e.g. Cloud Run reaped the request after a client
            # disconnect) — without it the await inside would itself cancel.
            await asyncio.shield(
                self._mark_task_done(
                    runner, run_request.user_id, run_request.session_id
                )
            )
            logger.info(
                "horizon.a2a.execute finished",
                extra={
                    "json_fields": {
                        "task_id": task_id,
                        "session_id": run_request.session_id,
                        "user_id": run_request.user_id,
                        "cancelled": cancelled,
                    }
                },
            )

    async def _mark_task_active(
        self,
        runner: Runner,
        user_id: str,
        session_id: str,
        task_id: str | None,
    ) -> None:
        if not task_id:
            return
        async with self._session_locks[(runner.app_name, user_id, session_id)]:
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
            if session is None:
                session = await runner.session_service.create_session(
                    app_name=runner.app_name,
                    user_id=user_id,
                    state={},
                    session_id=session_id,
                )
            existing = session.state.get(LHA_TASK_IDS_KEY) or []
            task_ids = (
                [*existing, task_id]
                if task_id not in existing
                else list(existing)
            )
            await runner.session_service.append_event(
                session,
                Event(
                    invocation_id=new_invocation_context_id(),
                    author="system",
                    actions=EventActions(
                        state_delta={
                            LHA_TASK_IDS_KEY: task_ids,
                            LHA_ACTIVE_TASK_KEY: task_id,
                        }
                    ),
                ),
            )

    async def _mark_task_done(
        self, runner: Runner, user_id: str, session_id: str
    ) -> None:
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            return
        await runner.session_service.append_event(
            session,
            Event(
                invocation_id=new_invocation_context_id(),
                author="system",
                actions=EventActions(state_delta={LHA_ACTIVE_TASK_KEY: None}),
            ),
        )
