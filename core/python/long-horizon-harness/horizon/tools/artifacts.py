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

"""Artifact extraction — bridge the per-user workspace and ADK's
``ArtifactService``.

A single ``artifact(action='save'|'load')`` dispatch:

* ``save``: hand a finished workspace file off to whatever artifact
  service the runner configured (``InMemoryArtifactService`` in unit/dev,
  ``GcsArtifactService`` in deploy).
* ``load``: pull a previously-saved artifact back into the workspace so
  a follow-up turn can edit it.

Without this, the only way to get a file out of the sandbox is to
``bash cat`` it and copy from stdout — fine for ASCII, useless for
anything binary.
"""

from __future__ import annotations

from typing import Any, Literal

from google.genai import types as genai_types

from horizon.environment_context import active_environment
from horizon.tools._artifact_links import artifact_url
from horizon.tools.file_ops import _resolve_under_env, guess_mime_from_bytes


async def _save(path: str, tool_context: Any, inline: bool) -> dict[str, Any]:
    target, err = _resolve_under_env(path)
    if err is not None:
        return {"success": False, "error": err}
    assert target is not None

    env = active_environment()
    try:
        data = await env.read_file(target)
    except FileNotFoundError:
        return {"success": False, "error": f"File not found: {path}"}
    except OSError as exc:
        return {"success": False, "error": f"Failed to read {path}: {exc}"}

    filename = target.name
    mime = guess_mime_from_bytes(filename, data)
    # display_name flows through ADK's genai→A2A converter into FilePart.name;
    # without it the UI receives name=None and falls back to "attachment.<ext>".
    part = genai_types.Part(
        inline_data=genai_types.Blob(
            data=data,
            mime_type=mime,
            display_name=filename,
        )
    )
    version = await tool_context.save_artifact(filename=filename, artifact=part)
    # ADK's include_artifacts_in_a2a_event interceptor only emits a FilePart
    # to the client when artifact_delta is populated. save_artifact() persists
    # the bytes but doesn't touch the delta — wire it up so the file streams
    # back through the A2A response.
    tool_context.actions.artifact_delta[filename] = version
    result: dict[str, Any] = {
        "success": True,
        "filename": filename,
        "version": version,
    }
    try:
        rel_path = str(target.relative_to(env.working_dir))
    except ValueError:
        rel_path = filename
    # Signed GCS URL when a bucket is configured (so GE can open it without our
    # frontend), else the same-origin download endpoint.
    session = tool_context.session
    result["url"] = artifact_url(
        app_name=session.app_name,
        user_id=session.user_id,
        session_id=session.id,
        filename=filename,
        version=version,
        rel_path=rel_path,
        mime=mime,
        inline=inline,
    )
    return result


async def _load(name: str, dest_path: str, tool_context: Any) -> dict[str, Any]:
    part = await tool_context.load_artifact(filename=name)
    if (
        part is None
        or part.inline_data is None
        or part.inline_data.data is None
    ):
        return {"success": False, "error": f"Artifact not found: {name}"}

    dest, err = _resolve_under_env(dest_path)
    if err is not None:
        return {"success": False, "error": err}
    assert dest is not None

    try:
        await active_environment().write_file(dest, part.inline_data.data)
    except OSError as exc:
        return {
            "success": False,
            "error": f"Failed to write {dest_path}: {exc}",
        }

    return {"success": True, "filename": name, "dest_path": str(dest)}


async def artifact(
    action: Literal["save", "load"],
    *,
    path: str | None = None,
    name: str | None = None,
    dest_path: str | None = None,
    inline: bool = True,
    tool_context: Any,
) -> dict[str, Any]:
    """Save a workspace file to the artifact service, or load one back.
    `action` picks the operation:

    - `save`: shows the file to the user (rendered inline where supported,
      else a link). Requires `path`. Stored under its basename only. The
      link appears automatically — do NOT write your own link, URL, or a
      "here's your file" line. For rich/visual output (charts, dashboards,
      tables), write a self-contained HTML file first: inline CSS, prefer
      static SVG over JS, no external requests. `inline=False` for
      JS-dependent HTML (downloads instead of inline-rendering; scripts
      don't run in the viewer).
    - `load`: writes a previously-saved artifact back into the workspace.
      Requires `name` (from `save`) and `dest_path` (where in the workspace to
      drop it).
    """
    if action == "save":
        if path is None:
            return {"success": False, "error": "action='save' requires 'path'"}
        return await _save(path, tool_context, inline)
    if action == "load":
        if name is None:
            return {"success": False, "error": "action='load' requires 'name'"}
        if dest_path is None:
            return {
                "success": False,
                "error": "action='load' requires 'dest_path'",
            }
        return await _load(name, dest_path, tool_context)
    return {
        "success": False,
        "error": f"Unknown action {action!r}. Use: save, load.",
    }


__all__ = ["artifact"]
