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

"""``read(path)`` — read a text file (paginated), or load it as a
multimodal ``Part`` for the agent's own next-turn context.

This is a ``BaseTool``, not a plain function tool, because it implements
``process_llm_request`` to inject the media Part on the *next* turn from
pending state. Text-vs-media routing is always auto-detected by MIME type
(``_default_as_media`` / ``_MEDIA_MIME_ALLOWLIST``); there is no manual
override parameter.

Guard order matters: the credential deny-list and non-regular-file check
run on both branches before any read, but the binary-extension check runs
only on the text branch, after the media/text dispatch decision. Running it
before dispatch would refuse every image (``.png``/``.jpg`` are binary
extensions; ``.pdf`` is not).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from google.adk.tools.base_tool import BaseTool
from google.genai import types as genai_types
from typing_extensions import override

from horizon.environment_context import active_environment
from horizon.models.media import b64_len
from horizon.models.registry import model_capabilities
from horizon.models.selector import resolve_model_name
from horizon.tools import names
from horizon.tools.file_ops import (
    _has_binary_extension,
    _is_write_denied,
    guess_mime,
    guess_mime_from_bytes,
    read_file,
)
from horizon.workspace_window import resolve_in_window, window_dirs

if TYPE_CHECKING:
    from google.adk.models import LlmRequest
    from google.adk.tools.tool_context import ToolContext


# Internal to this module: carries filenames staged for next-turn injection
# across the run_async / process_llm_request boundary (session state can't
# hold raw bytes, only JSON-serializable values).
_PENDING_STATE_KEY = "_pending_media_reads"


# Positive allowlist of formats Gemini can decode as inline media, not a
# blocklist over image/*|audio/*|video/*: image/svg+xml (XML markup, not a
# raster image) and audio/x-mpegurl (.m3u playlists, no audio bytes) are
# mime-typed as media but aren't. This is the sole routing gate; no override
# parameter exists.
_MEDIA_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
        "image/gif",
        "image/bmp",
        "image/tiff",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/x-flac",
        "audio/aac",
        "audio/x-aac",
        "audio/mp4",
        "audio/x-m4a",
        "audio/opus",
        "video/mp4",
        "video/quicktime",
        "video/x-msvideo",
        "video/x-matroska",
        "video/webm",
        "video/x-ms-wmv",
        "video/x-flv",
        "video/mpeg",
        "application/pdf",
    }
)


def _default_as_media(path: str) -> bool:
    """Auto-detect routing by guessed MIME type (extension-only, no I/O).

    Only formats in _MEDIA_MIME_ALLOWLIST default to the media path; every
    other mime, including text-ish ones that merely live under an image/
    audio/video top-level type (image/svg+xml, audio/x-mpegurl), stays text.
    """
    return guess_mime(path) in _MEDIA_MIME_ALLOWLIST


class ReadTool(BaseTool):
    """Read a workspace file as text, or load it into the agent's own
    context as a multimodal input."""

    def __init__(self) -> None:
        super().__init__(
            name=names.READ,
            description=(
                "Read a workspace file: text is returned paginated "
                "(offset/limit); images, PDFs, audio, and video load "
                "automatically as multimodal input into your OWN next "
                "turn. Paths: see routing."
            ),
        )

    def _get_declaration(self) -> genai_types.FunctionDeclaration | None:
        return genai_types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "path": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="Path of the file to read.",
                    ),
                    "offset": genai_types.Schema(
                        type=genai_types.Type.INTEGER,
                        description=(
                            "1-indexed line to start from (text files only). Default 1."
                        ),
                    ),
                    "limit": genai_types.Schema(
                        type=genai_types.Type.INTEGER,
                        description=(
                            "Max lines to return (text files only). Default 500."
                        ),
                    ),
                },
                required=["path"],
            ),
        )

    @override
    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"success": False, "error": "missing required arg 'path'"}

        target, err = resolve_in_window(
            path,
            active_environment().working_dir.resolve(),
            window_dirs(getattr(tool_context, "state", None)),
        )
        if err is not None:
            return {"success": False, "error": err}
        assert target is not None

        # Credential deny-list — both branches. The old media-only tool
        # never had this at all, so a denied path routed to the media
        # branch would otherwise both save the bytes as an artifact and
        # inject them into context.
        if _is_write_denied(str(target)):
            return {
                "success": False,
                "error": f"Read denied: {path} is on the protected paths list.",
            }

        # Non-regular-file rejection — both branches. Path.read_bytes on a
        # FIFO or /dev/urandom never returns; the old media-only tool
        # never guarded this.
        if os.path.lexists(target) and not os.path.isfile(target):
            if os.path.isdir(target):
                return {
                    "success": False,
                    "error": f"Path is a directory, not a file: {path}",
                }
            return {
                "success": False,
                "error": f"Not a regular file: {path}",
            }

        as_media = _default_as_media(str(target))

        if as_media:
            return await self._read_as_media(target, path, tool_context)

        # Binary-extension check: text branch ONLY, and only after dispatch.
        # .png/.jpg/.mp3/.mp4 are in this set but .pdf is not — checking it
        # before dispatch would refuse every image while a PDF test stayed
        # green.
        if _has_binary_extension(str(target)):
            return {
                "success": False,
                "error": f"Cannot read binary file: {path}",
            }

        # read_file() re-resolves and re-checks internally; a harmless
        # double-check that keeps the retained helper fully self-contained
        # for the 86 existing file_ops tests that call it directly.
        return await read_file(
            path,
            offset=args.get("offset") or 1,
            limit=args.get("limit") or 500,
            tool_context=tool_context,
        )

    async def _read_as_media(
        self, target: Any, path: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        try:
            data = await active_environment().read_file(target)
        except FileNotFoundError:
            return {"success": False, "error": f"File not found: {path}"}
        except OSError as exc:
            return {
                "success": False,
                "error": f"Failed to read {path}: {exc}",
            }

        filename = target.name
        mime_type = guess_mime_from_bytes(filename, data)
        part = genai_types.Part(
            inline_data=genai_types.Blob(
                data=data,
                mime_type=mime_type,
                display_name=filename,
            )
        )
        version = await tool_context.save_artifact(
            filename=filename, artifact=part
        )
        # Populating artifact_delta is what makes the A2A interceptor surface
        # the FilePart in the chat — so the user sees what the agent loaded.
        tool_context.actions.artifact_delta[filename] = version

        result: dict[str, Any] = {
            "success": True,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": len(data),
        }

        model_name = resolve_model_name(getattr(tool_context, "state", None))
        caps = model_capabilities(model_name)
        if not caps.can_view_mime(mime_type):
            result["model_can_view"] = False
            result["note"] = (
                f"Surfaced to the UI but NOT loaded into your context: the "
                f"active model ({model_name}) cannot view {mime_type}. "
                "Re-reading this path will not help — routing is "
                "extension-based, not model-based, so it always retries the "
                "same media path. Tell the user you can't view this file "
                "type with the current model."
            )
            return result

        if (
            caps.max_image_bytes is not None
            and mime_type.startswith("image/")
            and b64_len(len(data)) > caps.max_image_bytes
        ):
            cap_mb = caps.max_image_bytes / (1024 * 1024)
            result["too_large"] = True
            result["note"] = (
                f"Surfaced to the UI but NOT loaded into your context: this "
                f"image is {len(data)} bytes, over the active model's "
                f"{cap_mb:.0f} MB per-image limit. Resize or compress it "
                f"under {cap_mb:.0f} MB, then read it again."
            )
            return result

        env_root = active_environment().working_dir.resolve()
        try:
            label = str(target.resolve().relative_to(env_root))
        except ValueError:
            label = target.name

        pending = list(tool_context.state.get(_PENDING_STATE_KEY, []))
        pending.append(
            {"filename": filename, "version": version, "label": label}
        )
        tool_context.state[_PENDING_STATE_KEY] = pending
        return result

    @override
    async def process_llm_request(
        self, *, tool_context: ToolContext, llm_request: LlmRequest
    ) -> None:
        await super().process_llm_request(
            tool_context=tool_context, llm_request=llm_request
        )

        pending = tool_context.state.get(_PENDING_STATE_KEY)
        if not pending:
            return
        # Clear regardless of load outcome: never re-inject the same file.
        tool_context.state[_PENDING_STATE_KEY] = []

        for entry in pending:
            if isinstance(entry, dict):
                fn = entry.get("filename")
                ver = entry.get("version")
                lbl = entry.get("label") or fn
            elif isinstance(entry, str):
                fn = entry
                ver = None
                lbl = entry
            else:
                continue
            if not fn:
                continue
            part = await tool_context.load_artifact(filename=fn, version=ver)
            if part is None:
                continue
            llm_request.contents.append(
                genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part.from_text(text=f"Contents of {lbl}:"),
                        part,
                    ],
                )
            )


__all__ = ["ReadTool"]
