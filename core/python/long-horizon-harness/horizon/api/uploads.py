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

"""Workspace HTTP surface for the chat sidebar.

Routes mounted under ``/lha``:
  POST   /lha/uploads             — write a file to ``/workspace/uploads/``
                                    (folder uploads pass ``relpath`` so the
                                    tree is preserved under ``uploads/``)
  POST   /lha/workspace/folder    — create a top-level workspace folder (project)
  GET    /lha/workspace/tree      — list a directory within the workspace
  GET    /lha/workspace/download  — stream a file back, or a zip if path is a dir
  PUT    /lha/workspace/file      — overwrite an existing text file's contents
  DELETE /lha/workspace/file      — unlink a file, or ``rmtree`` if ``recursive=true``

All routes run outside an agent turn, so they resolve the user's
environment directly via ``session_start._ensure_environment`` (the
``active_environment()`` ContextVar is only set by
``on_session_start_callback`` and lives for the duration of one turn).

Listing/deletion/zip go through the ``Environment`` abstraction polymorphically
(``env.list_directory`` / ``env.delete_file`` / ``env.download_zip``); the
``on_host_fs`` capability flag gates the host-only ``Path.is_dir()``
short-circuits.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import BaseModel

from horizon.auth import current_user_id
from horizon.conversation import session_start
from horizon.environment import Environment
from horizon.tools.file_ops import _has_binary_extension, _is_write_denied

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Browser-side editing is for prose/code, not blobs. Anything larger is a
# download-and-edit-locally case, not a textarea case.
MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_FILENAME_LEN = 255
MAX_TREE_ENTRIES = 500
WORKSPACE_ROOT = "/workspace"
UPLOADS_SUBDIR = "uploads"

# Cache / tooling junk that pollutes the sidebar tree. Hidden from the UI
# listing only — the files still exist on disk and tools can still touch
# them.
_LISTING_EXCLUDE_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".DS_Store",
        "Thumbs.db",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ty_cache",
        ".ipynb_checkpoints",
        ".idea",
        ".vscode",
        ".next",
        ".turbo",
    }
)
_LISTING_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def _is_listing_junk(name: str) -> bool:
    if name in _LISTING_EXCLUDE_NAMES:
        return True
    return name.endswith(_LISTING_EXCLUDE_SUFFIXES)


def _sanitize_relpath(raw: str) -> PurePosixPath:
    """Validate a folder-upload relative path. Returns a ``PurePosixPath``
    rooted nowhere. Rejects absolute paths, traversal, control chars, and
    components that fail ``_sanitize_filename`` per-segment."""
    if not raw:
        raise HTTPException(status_code=400, detail="relpath is empty")
    if "\x00" in raw:
        raise HTTPException(
            status_code=400, detail="relpath must not contain null bytes"
        )
    if any(ord(c) < 32 for c in raw):
        raise HTTPException(
            status_code=400,
            detail="relpath must not contain control characters",
        )
    # Normalise backslashes (Windows browsers can submit either separator).
    normalised = raw.replace("\\", "/")
    if normalised.startswith("/"):
        raise HTTPException(status_code=400, detail="relpath must be relative")
    normalised = normalised.strip("/")
    if not normalised:
        raise HTTPException(status_code=400, detail="relpath is empty")
    parts = normalised.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise HTTPException(
                status_code=400, detail=f"relpath segment invalid: {part!r}"
            )
        # Reuse _sanitize_filename's per-segment rules (rejects null/control,
        # leading dots, over-long names — but we want to allow files that
        # already have a leading dot inside a folder, so we re-check by hand).
        if "/" in part or "\\" in part:
            raise HTTPException(
                status_code=400,
                detail="relpath segment must not contain separators",
            )
        if len(part) > MAX_FILENAME_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"relpath segment longer than {MAX_FILENAME_LEN} characters",
            )
    return PurePosixPath(*parts)


def _sanitize_filename(raw: str) -> str:
    """Return a safe basename or raise HTTPException(400).

    Rejects path separators, traversal, null bytes, control chars,
    leading dots, and over-long names. The sandbox shim's
    ``_resolve_within_workspace`` is the ultimate guard; this is the
    early reject so the user gets a clear 400 without a round-trip.
    """
    if not raw:
        raise HTTPException(status_code=400, detail="filename is empty")
    if "/" in raw or "\\" in raw:
        raise HTTPException(
            status_code=400, detail="filename must not contain path separators"
        )
    if "\x00" in raw:
        raise HTTPException(
            status_code=400, detail="filename must not contain null bytes"
        )
    if any(ord(c) < 32 for c in raw):
        raise HTTPException(
            status_code=400,
            detail="filename must not contain control characters",
        )
    if raw in (".", ".."):
        raise HTTPException(
            status_code=400, detail="filename must not be '.' or '..'"
        )
    if raw.startswith("."):
        raise HTTPException(
            status_code=400, detail="filename must not start with '.'"
        )
    if len(raw) > MAX_FILENAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"filename longer than {MAX_FILENAME_LEN} characters",
        )
    return raw


def _resolve_workspace_path(env: Environment, requested: str) -> Path:
    """Translate a UI-facing ``/workspace[/sub]`` path to an absolute path
    under ``env.working_dir``. Rejects anything that escapes the root."""
    if not requested:
        return env.working_dir

    rel: str
    if requested.startswith(WORKSPACE_ROOT):
        rel = requested[len(WORKSPACE_ROOT) :].lstrip("/")
    elif requested.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail=f"path must be under {WORKSPACE_ROOT}",
        )
    else:
        rel = requested

    target = (env.working_dir / rel).resolve() if rel else env.working_dir
    root = (
        env.working_dir.resolve()
        if env.working_dir.is_absolute()
        else env.working_dir
    )
    try:
        target.relative_to(root)
    except ValueError as err:
        raise HTTPException(
            status_code=400, detail=f"path escapes {WORKSPACE_ROOT}"
        ) from err
    return target


def _disposition(name: str, *, inline: bool = False) -> str:
    """Build a ``Content-Disposition`` header that survives non-ASCII
    filenames. Emits both the legacy ``filename=`` form (ASCII fallback)
    and the RFC 5987 ``filename*=`` form so old and new browsers agree.
    """
    ascii_fallback = (
        name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    )
    encoded = quote(name, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


# Mime types the workspace endpoint will serve inline. Everything else
# falls back to attachment so a malicious upload can't be coaxed into
# rendering with whatever the browser sniffs. CSP-sandbox is applied on
# top, so even HTML/SVG can't execute scripts in our origin.
_INLINE_MIME_PREFIXES = ("text/", "image/")
_INLINE_MIME_EXACT = frozenset({"application/pdf", "application/json"})


def _resolve_inline_mime(filename: str) -> str | None:
    """Return the safe inline mime for ``filename`` or ``None`` to force
    download. Falls back conservatively when ``mimetypes`` can't guess."""
    guessed, _ = mimetypes.guess_type(filename)
    if not guessed:
        return None
    if guessed in _INLINE_MIME_EXACT:
        return guessed
    if any(guessed.startswith(prefix) for prefix in _INLINE_MIME_PREFIXES):
        return guessed
    return None


def _to_workspace_path(env: Environment, target: Path) -> str:
    """Return the UI-facing ``/workspace[/...]`` label for ``target``.

    Resolves both sides so a symlinked working_dir (e.g. macOS ``/tmp``
    → ``/private/tmp``) doesn't fall through to leaking the host path.
    """
    root = env.working_dir.resolve()
    try:
        rel = target.resolve().relative_to(root)
    except ValueError:
        return str(target)
    if str(rel) in (".", ""):
        return WORKSPACE_ROOT
    return f"{WORKSPACE_ROOT}/{rel}".rstrip("/")


async def _list_directory(
    env: Environment, target: Path, *, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """List ``target`` via the env interface, hiding cache/tooling junk
    (``__pycache__``, ``.DS_Store``, etc.) from the sidebar regardless of
    backend."""
    entries, truncated = await env.list_directory(target, limit=limit)
    entries = [e for e in entries if not _is_listing_junk(e.get("name", ""))]
    return entries, truncated


async def _make_dir(env: Environment, target: Path) -> None:
    await env.make_dir(target)


async def _delete_path(
    env: Environment, target: Path, *, recursive: bool
) -> None:
    await env.delete_file(target, recursive=recursive)


async def _download_dir_zip(env: Environment, target: Path) -> bytes:
    return await env.download_zip(target)


async def _serve_dir_zip(env: Environment, target: Path) -> Response:
    try:
        data = await _download_dir_zip(env, target)
    except FileNotFoundError as err:
        raise HTTPException(
            status_code=404, detail=f"{target} not found"
        ) from err
    except PermissionError as err:
        raise HTTPException(status_code=403, detail=str(err)) from err
    name = target.name or "workspace"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": _disposition(f"{name}.zip")},
    )


class NewFolderBody(BaseModel):
    name: str


class WriteFileBody(BaseModel):
    content: str


def attach_uploads_routes(app: FastAPI) -> None:
    """Mount ``POST /lha/uploads``, ``POST /lha/workspace/folder``, and
    ``GET /lha/workspace/tree``.

    Endpoints resolve the user's env via ``session_start._ensure_environment``
    (the ``active_environment()`` ContextVar is only set during agent turns),
    so no Runner / session_service handle is needed here.
    """
    router = APIRouter(prefix="/lha", tags=["lha"])

    @router.post("/uploads")
    async def upload_file(
        file: Annotated[UploadFile, File(...)],
        user_id: Annotated[str, Depends(current_user_id)],
        relpath: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        # Folder uploads send a per-file relative path (e.g. "src/main.py").
        # Plain file uploads omit ``relpath`` and only validate the basename.
        if relpath:
            rel = _sanitize_relpath(relpath)
            sub_target = Path(*rel.parts)
        else:
            safe_name = _sanitize_filename(file.filename or "")
            sub_target = Path(safe_name)

        # Bounded read: cap at MAX_UPLOAD_BYTES+1 so an oversized payload
        # is rejected without loading the full body into memory.
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes",
            )
        size = len(data)

        env = await session_start._ensure_environment(user_id)
        target = env.working_dir / UPLOADS_SUBDIR / sub_target

        replaced = False
        try:
            await env.read_file(target)
            replaced = True
        except FileNotFoundError:
            replaced = False
        except Exception:
            # Treat any other read error as "couldn't tell" — fall through
            # to the write. The frontend uses ``replaced`` for messaging
            # only; failing the upload on a probe glitch would be worse.
            replaced = False

        await env.write_file(target, data)
        return {
            "path": _to_workspace_path(env, target),
            "size": size,
            "replaced": replaced,
        }

    @router.post("/workspace/folder")
    async def workspace_new_folder(
        body: NewFolderBody,
        user_id: Annotated[str, Depends(current_user_id)],
    ) -> dict[str, Any]:
        # A project is a top-level workspace folder — enforce a single segment.
        name = body.name.strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise HTTPException(status_code=400, detail="invalid folder name")
        env = await session_start._ensure_environment(user_id)
        target = _resolve_workspace_path(env, name)
        try:
            await _make_dir(env, target)
        except OSError as err:
            logger.warning("workspace mkdir failed for %s: %s", target, err)
            raise HTTPException(
                status_code=500, detail=f"folder create failed: {err}"
            ) from err
        return {"path": _to_workspace_path(env, target)}

    @router.get("/workspace/tree")
    async def workspace_tree(
        path: str = Query(WORKSPACE_ROOT),
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        env = await session_start._ensure_environment(user_id)
        target = _resolve_workspace_path(env, path)

        try:
            entries, truncated = await _list_directory(
                env, target, limit=MAX_TREE_ENTRIES
            )
        except FileNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"{path} not found"
            ) from err
        except NotADirectoryError as err:
            raise HTTPException(
                status_code=400, detail=f"{path} is not a directory"
            ) from err
        except PermissionError as err:
            raise HTTPException(status_code=403, detail=str(err)) from err
        except OSError as err:
            logger.warning("workspace listing failed for %s: %s", target, err)
            raise HTTPException(
                status_code=500, detail=f"workspace listing failed: {err}"
            ) from err

        return {
            "path": _to_workspace_path(env, target),
            "entries": entries,
            "truncated": truncated,
        }

    @router.get("/workspace/download")
    async def workspace_download(
        path: Annotated[str, Query(...)],
        user_id: Annotated[str, Depends(current_user_id)],
        inline: Annotated[bool, Query()] = False,
    ) -> Response:
        env = await session_start._ensure_environment(user_id)
        target = _resolve_workspace_path(env, path)

        # Sandbox-side existence/is-dir is established by the shim; for
        # LocalEnvironment we can short-circuit and pick the dir-zip branch
        # without round-tripping read_file just to swallow IsADirectoryError.
        is_dir_local = env.on_host_fs and target.is_dir()
        if is_dir_local:
            return await _serve_dir_zip(env, target)

        try:
            data = await env.read_file(target)
        except FileNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"{path} not found"
            ) from err
        except IsADirectoryError:
            # Sandbox path lands here when ``target`` is a directory in the
            # sandbox container.
            return await _serve_dir_zip(env, target)
        except PermissionError as err:
            raise HTTPException(status_code=403, detail=str(err)) from err

        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes",
            )

        inline_mime = _resolve_inline_mime(target.name) if inline else None
        if inline_mime is not None:
            return Response(
                content=data,
                media_type=inline_mime,
                headers={
                    "Content-Disposition": _disposition(
                        target.name, inline=True
                    ),
                    # CSP-sandbox neutralizes HTML/SVG scripts even if the
                    # browser ignores nosniff. nosniff stops content-type
                    # sniffing on text/* responses.
                    "Content-Security-Policy": "sandbox",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": _disposition(target.name)},
        )

    @router.put("/workspace/file")
    async def workspace_write(
        body: WriteFileBody,
        path: Annotated[str, Query(...)],
        user_id: Annotated[str, Depends(current_user_id)],
    ) -> dict[str, Any]:
        """Overwrite an existing workspace text file from the browser editor.

        Edit-only by design: creation stays on ``POST /lha/uploads`` so this
        route can't be used to drop new files anywhere under the workspace.
        Mirrors the agent's own write guards (``_is_write_denied`` protected
        paths, binary-extension reject) so the UI can't route around them.
        """
        data = body.content.encode("utf-8")
        if len(data) > MAX_EDIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"content exceeds {MAX_EDIT_BYTES} bytes",
            )

        env = await session_start._ensure_environment(user_id)
        target = _resolve_workspace_path(env, path)

        if _is_write_denied(str(target)):
            raise HTTPException(
                status_code=403,
                detail=f"{path} is on the protected paths list",
            )
        if _has_binary_extension(str(target)):
            raise HTTPException(
                status_code=415, detail=f"{path} is not a text file"
            )

        # Existence probe doubles as the is-a-directory guard: env.read_file
        # raises IsADirectoryError before we clobber anything.
        try:
            await env.read_file(target)
        except FileNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"{path} not found"
            ) from err
        except IsADirectoryError as err:
            raise HTTPException(
                status_code=400, detail=f"{path} is a directory"
            ) from err
        except OSError as err:
            raise HTTPException(
                status_code=500, detail=f"failed to read {path}: {err}"
            ) from err

        try:
            await env.write_file(target, data)
        except OSError as err:
            logger.warning("workspace write failed for %s: %s", target, err)
            raise HTTPException(
                status_code=500, detail=f"write failed: {err}"
            ) from err

        return {"path": _to_workspace_path(env, target), "size": len(data)}

    @router.delete("/workspace/file", status_code=204)
    async def workspace_delete(
        path: Annotated[str, Query(...)],
        user_id: Annotated[str, Depends(current_user_id)],
        recursive: Annotated[bool, Query()] = False,
    ) -> Response:
        env = await session_start._ensure_environment(user_id)
        target = _resolve_workspace_path(env, path)

        root = (
            env.working_dir.resolve()
            if env.working_dir.is_absolute()
            else env.working_dir
        )
        if target.resolve() == root:
            raise HTTPException(
                status_code=400,
                detail=f"refusing to delete workspace root {path}",
            )

        # Local-tier pre-check: macOS unlink on a directory raises EPERM
        # (not EISDIR), so without this the OS error wouldn't reliably map
        # to 400 when the caller forgot ``recursive=true``. Sandbox-tier
        # runs Linux and the shim enforces the same check.
        if env.on_host_fs and not recursive and target.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"{path} is a directory; pass recursive=true",
            )

        try:
            await _delete_path(env, target, recursive=recursive)
        except FileNotFoundError as err:
            raise HTTPException(
                status_code=404, detail=f"{path} not found"
            ) from err
        except IsADirectoryError as err:
            raise HTTPException(
                status_code=400,
                detail=f"{path} is a directory; pass recursive=true",
            ) from err
        except PermissionError as err:
            raise HTTPException(status_code=403, detail=str(err)) from err
        except OSError as err:
            logger.warning("workspace delete failed for %s: %s", target, err)
            raise HTTPException(
                status_code=500, detail=f"workspace delete failed: {err}"
            ) from err

        return Response(status_code=204)

    app.include_router(router)
