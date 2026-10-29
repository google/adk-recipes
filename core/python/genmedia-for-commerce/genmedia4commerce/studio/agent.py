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

"""The Studio agent, the app's one ADK agent (``root_agent``, ``app``).

One definition, two ways to run, one kind of session: the conversation is
the ADK session of the studio, stored in the media bucket
(``studio.sessions``), so every instance and every turn sees all of it,
with the ``[studio]`` facts the app adds (``studio.history.note``).

- **Panel** (the ``studio_agent`` MCP tool, later the web app's agentic
  mode): ``run_agent_turn`` sets the turn's scope (``studio.turn``) and
  runs the agent on the session whose id is the studio id; the tools
  return patches for the panel; the state is saved at the end.
- **Plain ADK** (``adk web``, the ADK API, ``adk eval``): no caller sets a
  scope, so ``_before_agent`` makes a ``Headless`` one for the studio of
  the ADK session and ``_after_agent`` saves it. Finished results are saved
  as ADK artifacts.

In both, ``_StudioPlugin`` rewrites the user's message before it is
stored: attached files become ``[studio]`` lines (no image bytes are ever
stored), and the current studio and its images (``gs://`` file parts) are
added when they changed since they were last sent. The system instruction
stays the same from turn to turn, so the model's prefix cache can hit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from genmedia4commerce.core.errors import USER_ERRORS, user_message
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.settings import export_genai_env
from genmedia4commerce.studio import sessions, store, turn
from genmedia4commerce.studio import state as ss
from genmedia4commerce.studio.agent_tools import TOOLS
from genmedia4commerce.studio.headless import (
    Headless,
    save_artifacts,
    without_attachments,
)
from genmedia4commerce.studio.prompt import (
    IMAGES_NOTE,
    image_parts,
    instruction,
    summary,
    turn_parts,
)

__all__ = [
    "APP_NAME",
    "app",
    "image_parts",
    "root_agent",
    "run_agent_turn",
    "summary",
]

logger = logging.getLogger(__name__)

APP_NAME = sessions.APP_NAME
#: What the model reads of a tool's unexpected failure (``_on_tool_error``).
_FAILED = "an unexpected error on the server; try again"
#: When the model cannot read the gs:// images, the call is retried without
#: them and no images are sent for this long.
_IMAGES_OFF_S = 600.0
_IMAGES = {"off_until": 0.0}


def _images_on() -> bool:
    return time.monotonic() >= _IMAGES["off_until"]


async def _scope(studio_id: str, kind: type[turn.Scope]) -> Any:
    """A turn's scope: the studio's state and latest results, read once off
    the event loop."""
    state, latest = await asyncio.gather(
        asyncio.to_thread(ss.get_state, studio_id),
        asyncio.to_thread(ss.last_results, studio_id),
    )
    return kind(studio_id=studio_id, st=state, results=latest)


async def _turn_studio(session_id: str) -> turn.Scope:
    """The panel's scope, or (plain ADK, before ``_before_agent``) a
    snapshot of the session's studio."""
    with contextlib.suppress(LookupError):
        if not isinstance(sc := turn.current(), Headless):
            return sc
    return await _scope(store.check_id(session_id), Headless)


class _StudioPlugin(BasePlugin):
    """Rewrites the user's message before ADK stores it (both modes)."""

    def __init__(self) -> None:
        super().__init__(name="genmedia_studio")

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        """Attachments become ``[studio]`` lines; the current studio and its
        images are added when they changed since they were last sent."""
        if any(p.function_response for p in user_message.parts or []):
            return None
        session = invocation_context.session
        sc = await _turn_studio(session.id)
        msg = await without_attachments(sc.studio_id, user_message)
        extra = turn_parts(
            session.events, sc.st, sc.results, images=_images_on()
        )
        return types.Content(role="user", parts=[*(msg.parts or []), *extra])


async def _instruction(_ctx: ReadonlyContext) -> str:
    headless = isinstance(turn.current(), Headless)
    return await asyncio.to_thread(instruction, headless=headless)


def _image_access_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "gs://" in text or ("permission" in text and "storage" in text)


def _drop_images(llm_request: LlmRequest) -> bool:
    """Remove the studio images from the request; whether there were any."""
    found = False
    for content in llm_request.contents:
        parts = content.parts or []
        kept = [p for p in parts if not p.file_data and p.text != IMAGES_NOTE]
        found = found or len(kept) < len(parts)
        content.parts = kept
    return found


async def _before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    """While the model cannot read the images, send none of them."""
    del callback_context
    if not _images_on():
        _drop_images(llm_request)


async def _on_model_error(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse | None:
    """When the model cannot read the studio images, call it again without
    them (and send none for a while); other errors go on as they are."""
    del callback_context
    if not (_image_access_error(error) and _drop_images(llm_request)):
        return None
    logger.warning("Model cannot read studio images (%s); retrying", error)
    _IMAGES["off_until"] = time.monotonic() + _IMAGES_OFF_S
    model = root_agent.canonical_model
    replies = [r async for r in model.generate_content_async(llm_request)]
    return replies[-1] if replies else None


async def _log_usage(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> None:
    """Log the prompt tokens of a model call and how many were cached."""
    del callback_context
    usage = llm_response.usage_metadata
    if usage and not llm_response.partial:
        logger.info(
            "Studio model call: %s prompt tokens, %s cached",
            usage.prompt_token_count,
            usage.cached_content_token_count or 0,
        )


async def _after_tool(
    tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: Any
) -> None:
    """In a headless turn, save a started job at once (ADK skips
    ``_after_agent`` when the turn fails later) and finished results as
    artifacts."""
    del args
    if not isinstance(sc := turn.current(), Headless):
        return
    response = tool_response if isinstance(tool_response, dict) else {}
    if tool.name == "generate" and "job_id" in response:
        # Idempotent: the end-of-turn save sets the same job again.
        job = {
            "job_started": {k: response[k] for k in ("job_id", "capability")}
        }
        await asyncio.to_thread(ss.save_patched, sc.studio_id, [job])
    if tool.name == "check_generation" and "results" in response:
        await save_artifacts(tool_context, response)


async def _on_tool_error(
    tool: BaseTool, args: dict, tool_context: ToolContext, error: Exception
) -> dict[str, Any]:
    """Answer a failed tool call with its error, so every call gets a
    response (Vertex rejects a history with an unanswered call). The model
    reads only a message meant for the user (``errors.user_message``), never
    paths or internals; the details are logged."""
    del args, tool_context
    if isinstance(error, USER_ERRORS):
        logger.warning("Studio tool %s failed: %s", tool.name, error)
    else:
        logger.error("Studio tool %s failed", tool.name, exc_info=error)
    return {"error": f"{tool.name} failed: {user_message(error, _FAILED)}"}


async def _before_agent(callback_context: CallbackContext) -> None:
    """Start a headless turn unless ``run_agent_turn`` set the scope: the
    studio id is the ADK session id, which must be a valid studio id
    (``store.check_id``: ``InputError`` otherwise, never rewritten)."""
    with contextlib.suppress(LookupError):
        if not isinstance(turn.current(), Headless):
            return
    sid = store.check_id(callback_context.session.id)
    scope = await _scope(sid, Headless)
    scope.token = turn.start(scope)


async def _after_agent(callback_context: CallbackContext) -> None:
    """Save what a headless turn changed and close it."""
    del callback_context
    sc = turn.current()
    if not isinstance(sc, Headless):
        return
    if sc.out.patches:
        sc.out.saved_at = await asyncio.to_thread(
            ss.save_patched, sc.studio_id, sc.out.patches
        )
    if sc.token is not None:  # set by ``_before_agent``
        turn.end(sc.token)


export_genai_env()

root_agent = LlmAgent(
    name="genmedia_studio",
    model=Gemini(
        model=get_models().studio_agent,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=_instruction,
    # Choosing 1 to 3 tool calls needs little reasoning: with the model's
    # default thinking one turn took 2 to 4 minutes.
    generate_content_config=types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="LOW")
    ),
    tools=list(TOOLS),
    before_agent_callback=_before_agent,
    after_agent_callback=_after_agent,
    before_model_callback=_before_model,
    after_model_callback=_log_usage,
    on_model_error_callback=_on_model_error,
    after_tool_callback=_after_tool,
    on_tool_error_callback=_on_tool_error,
)

app = App(name=APP_NAME, root_agent=root_agent, plugins=[_StudioPlugin()])

_SESSIONS = sessions.StudioSessionService()


async def _run(message: str, studio_id: str) -> list[str]:
    """One runner pass on the studio's session (created if missing)."""
    await asyncio.to_thread(sessions.ensure_session, studio_id)
    runner = Runner(app=app, session_service=_SESSIONS)
    msg = types.Content(role="user", parts=[types.Part(text=message)])
    texts: list[str] = []
    async for ev in runner.run_async(
        user_id=sessions.PANEL_USER, session_id=studio_id, new_message=msg
    ):
        if ev.is_final_response() and ev.content and ev.content.parts:
            texts += [p.text for p in ev.content.parts if p.text]
    return texts


def _for_log(value: str) -> str:
    """``value`` without line breaks, so a client-sent id cannot forge log
    lines (the MCP tools pass the studio id as the client sent it)."""
    return value.replace("\r", "").replace("\n", "")


def _fallback_reply(patches: list[dict[str, Any]]) -> str:
    """A reply for a turn whose model answer had no text (it happens after
    a tool call), from what the turn did, so the user never sees a blank."""
    if any("job_started" in p for p in patches):
        return (
            "I've started the generation. The results appear in the studio "
            "as soon as they are checked."
        )
    if patches:
        return "I've updated the studio. Shall I generate?"
    return "Sorry, I lost my reply. Could you say that again?"


async def run_agent_turn(
    message: str, studio_id: str
) -> tuple[str, turn.AgentTurn]:
    """Run the agent on one message from the panel; return the studio id and
    what the agent did."""
    studio_id = studio_id or uuid.uuid4().hex[:12]
    scope = await _scope(studio_id, turn.Scope)
    token = turn.start(scope)
    try:
        texts = await _run(message, studio_id)
        scope.out.reply = " ".join(texts).strip()
        if not scope.out.reply:
            logger.warning(
                "Studio agent turn for %s had no text", _for_log(studio_id)
            )
            scope.out.reply = _fallback_reply(scope.out.patches)
    except Exception:
        # Keep what the tools already changed: the app still gets the patches.
        logger.exception("Studio agent turn failed for %s", _for_log(studio_id))
        if not scope.out.patches:
            raise
        scope.out.reply = "I updated the studio, but could not finish my reply."
    finally:
        turn.end(token)
    # Patch the saved state rather than overwrite it, so changes the panel
    # made during the turn are kept.
    scope.out.saved_at = await asyncio.to_thread(
        ss.save_patched, studio_id, scope.out.patches
    )
    return studio_id, scope.out
