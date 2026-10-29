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

"""The GenMedia Studio as one MCP App (``ui://genmedia/studio``).

Gemini Enterprise renders the app in a sandboxed iframe. Every click inside
it is local or a silent ``tools/call``, so nothing appears in the chat and
no model answers it.

- **Assistant tool**: ``studio_agent``, the only tool the host's model sees,
  opens the app and relays the user's message to our ADK Studio agent
  (``studio/agent.py``). It saves what the agent changed and returns the
  saved ``state`` with its ``updated_at`` (and, for older panels, the
  ``patches``). Gemini Enterprise routes a later call for the same resource
  into the open app, which shows that state when it is newer than its own.
- **App tools**: ``studio_event`` applies each user action to the saved
  studio (``studio.state``, which the server owns), notes it in the agent's
  history and returns the new state for the app to render; ``studio_state``
  restores it; ``studio_gallery``, ``studio_upload``,
  ``studio_catalog_search``, ``studio_preview`` and ``studio_job_*`` serve the
  panel.

Each tool wraps one operation of ``studio.service``, which the web app's
REST API (``api/studio.py``) shares.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import mcp.types as mt
from mcp.server.fastmcp import FastMCP

from genmedia4commerce.core.errors import USER_ERRORS, InputError, user_message
from genmedia4commerce.studio import service, store
from genmedia4commerce.studio.session_limits import MAX_MESSAGE, TOO_LONG

__all__ = ["STUDIO_URI", "build_server"]

logger = logging.getLogger(__name__)

STUDIO_URI = "ui://genmedia/studio"
_HTML = Path(__file__).with_name("static") / "studio.html"
_META = {"ui": {"resourceUri": STUDIO_URI}}

_INSTRUCTIONS = """\
GenMedia Studio creates product media: Photo Try-On (a person wearing
products), Video Try-On (the try-on, animated) and 360° Product Spin.
"Try-on", "VTO" or "virtual try-on" means Photo Try-On; Video Try-On only
when the user asks for a video (a video, an animation, "video VTO").
For EVERY request about the studio or product media, including just
opening it (photo try-on / VTO, video try-on, 360 spin, pick a model, find
or swap products, the scene and style, generate, questions about the
results), call studio_agent with the user's message word for word and the
studio_id from the studio's model context (empty if there is none yet).
Then show the user the agent's reply. Do not answer studio requests yourself.
Files attached in this chat do not reach the studio: still call studio_agent
(it asks the user to use Upload in the studio).
"""


def _result(payload: dict[str, Any]) -> mt.CallToolResult:
    """A tool result: the app reads ``structuredContent``, the model the text."""
    shown = ("studio_id", "reply", "error", "ok")
    summary = {k: v for k, v in payload.items() if k in shown}
    return mt.CallToolResult(
        content=[
            mt.TextContent(type="text", text=json.dumps(summary, default=str))
        ],
        structuredContent=payload,
        isError="error" in payload and len(payload) == 1,
        _meta=_META,
    )


_Tool = Callable[..., Awaitable[mt.CallToolResult]]


def _check_studio_id(studio_id: str) -> None:
    """``InputError`` for an invalid studio id ("" is none yet)."""
    if studio_id:
        store.check_id(studio_id)


def _guarded(fn: _Tool) -> _Tool:
    """``fn`` returning an ``{"error"}`` result instead of raising.

    FastMCP would answer an exception with plain text, which the app cannot
    parse. Callers read only a message meant for the user
    (``errors.user_message``), never a traceback, a bucket or a setting.
    """

    @functools.wraps(fn)
    async def tool(*args: Any, **kwargs: Any) -> mt.CallToolResult:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            if isinstance(exc, USER_ERRORS):
                logger.warning("%s: %s", fn.__name__, exc)
            else:
                logger.exception("%s failed", fn.__name__)
            failed = f"{fn.__name__} failed; try again."
            return _result({"error": user_message(exc, failed)})

    return tool


# ---- assistant tool ---------------------------------------------------------


@_guarded
async def studio_agent(message: str, studio_id: str = "") -> mt.CallToolResult:
    """Open the GenMedia Studio and act on it. Use it for EVERY request
    about the studio or product media: opening it, photo try-on / VTO, video
    try-on, 360 spin, picking models, finding or swapping products, the scene
    and style, generating, questions about results. Pass the user's message
    word for word.

    Args:
        message: The user's message, word for word.
        studio_id: The studio_id from the studio's model context, or "".
    """
    _check_studio_id(studio_id)
    if len(message) > MAX_MESSAGE:
        raise InputError(TOO_LONG)
    return _result(await service.agent_turn(message, studio_id))


# ---- app tools ---------------------------------------------------------------


@_guarded
async def studio_event(
    studio_id: str, event: dict[str, Any]
) -> mt.CallToolResult:
    """App only: apply one user action to the saved studio (an op of
    ``studio.state``, or {"job_finished": {"job_id"}}) and note it for the
    agent; returns the new state and its updated_at.

    Args:
        studio_id: The app's studio id.
        event: The action, e.g. {"set": {"field": "person", "item": {"label",
            "ref"}}, "source": "gallery"}.
    """
    store.check_id(studio_id)
    return _result(await asyncio.to_thread(service.event, studio_id, event))


@_guarded
async def studio_state(studio_id: str) -> mt.CallToolResult:
    """App only: the saved state of a studio, to restore the panel when a
    conversation is reopened: images, settings, results (with new preview
    URLs) and the running generation, if any.

    Args:
        studio_id: The studio id.
    """
    store.check_id(studio_id)
    return _result(await asyncio.to_thread(service.state, studio_id))


@_guarded
async def studio_preview(refs: list[str]) -> mt.CallToolResult:
    """App only: browser URLs for image refs without a thumbnail.

    Args:
        refs: gs:// or gallery refs.
    """
    return _result(await asyncio.to_thread(service.previews, refs))


@_guarded
async def studio_gallery() -> mt.CallToolResult:
    """App only: preset models and products with thumbnails."""
    return _result(await asyncio.to_thread(service.gallery))


@_guarded
async def studio_upload(
    image_base64: str, filename: str = "", studio_id: str = ""
) -> mt.CallToolResult:
    """App only: save an uploaded image; returns its ref, thumbnail and a
    one-sentence description.

    Args:
        image_base64: The image, base64 or a data URL.
        filename: The original file name.
        studio_id: The app's studio id (groups its uploads and runs).
    """
    store.check_id(studio_id)
    return _result(
        await asyncio.to_thread(
            service.upload, image_base64, filename, studio_id
        )
    )


@_guarded
async def studio_catalog_search(
    query: str, audience: str = "", studio_id: str = ""
) -> mt.CallToolResult:
    """App only: catalogue search results with thumbnails (the search is
    noted for the agent).

    Args:
        query: What to look for.
        audience: "women", "men", "unisex" or "".
        studio_id: The app's studio id.
    """
    _check_studio_id(studio_id)
    return _result(await service.catalog_search(query, audience, studio_id))


@_guarded
async def studio_job_start(spec: dict[str, Any]) -> mt.CallToolResult:
    """App only: start a generation from the studio state; returns a job id.

    Args:
        spec: studio_id, capability, person, reference_model, products,
            tryon_image, back_image, spin (refs), source_run (the run
            record of the try-on that made tryon_image) and settings.
    """
    store.check_id(spec.get("studio_id") if isinstance(spec, dict) else None)
    return _result(await service.job_start(spec))


@_guarded
async def studio_job_status(job_id: str) -> mt.CallToolResult:
    """App only: status and progress of a generation, with the results so
    far (each variation as soon as it is judged).

    Args:
        job_id: The id from studio_job_start.
    """
    store.check_job_id(job_id)
    return _result(await asyncio.to_thread(service.job_status, job_id))


# Every tool is marked read-only on purpose, even those that write: Gemini
# Enterprise asks the user to confirm each call to a tool that is not
# read-only, and the panel's calls must stay silent.
_READ_ONLY = mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False)
#: Called by Gemini Enterprise's model (and by the app).
_MODEL_TOOLS = (studio_agent,)
#: Called by the app only. MCP Apps ``visibility: ["app"]`` keeps them out of
#: the host model's tool list, so studio_agent is its only tool.
_APP_TOOLS = (
    studio_event,
    studio_state,
    studio_preview,
    studio_gallery,
    studio_upload,
    studio_catalog_search,
    studio_job_start,
    studio_job_status,
)


def _tool_meta(visibility: list[str]) -> dict[str, Any]:
    return {"ui": {**_META["ui"], "visibility": visibility}}


def build_server() -> FastMCP:
    """A new Studio MCP server (one per app: each owns its session manager)."""
    server = FastMCP("GenMedia Studio", instructions=_INSTRUCTIONS)
    server.resource(
        STUDIO_URI,
        name="genmedia_studio",
        title="GenMedia Studio",
        description="Photo Try-On, Video Try-On and 360° Product Spin studio.",
        mime_type="text/html;profile=mcp-app",
        meta={
            "ui": {
                "prefersBorder": False,
                "csp": {"resourceDomains": ["https://storage.googleapis.com"]},
            }
        },
    )(lambda: _HTML.read_text(encoding="utf-8"))
    for group, visibility in (
        (_MODEL_TOOLS, ["model", "app"]),
        (_APP_TOOLS, ["app"]),
    ):
        meta = _tool_meta(visibility)
        for fn in group:
            server.tool(name=fn.__name__, annotations=_READ_ONLY, meta=meta)(fn)
    return server
