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

"""Save a thread's workspace files to the user's Google Drive.

Uploads use the user's server-held OAuth token into `ADK Voice Agent/<project title>`
and tag each file's `appProperties` with a hash of `(project, path)` so re-saving
updates the existing Drive file in place without a database table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import httpx

from app.auth.google_apis import DRIVE_FILES_URL, DRIVE_UPLOAD_URL
from app.auth.providers import GRANT_ACCESS_HINT
from app.user_settings import SAVE_TO_DRIVE, user_settings
from app.workers import policy
from app.workers.workspace import ProjectKind, project_kind

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.threads.model import ThreadHandle

logger = logging.getLogger(__name__)

SAVE_ON, SAVE_OFF = "on", "off"
SAVE_CHOICES = (SAVE_ON, SAVE_OFF)
ROOT_FOLDER = "ADK Voice Agent"
MAX_UPLOAD_BYTES = 10_000_000
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_MIME = {
    "doc": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "slides": "application/vnd.google-apps.presentation",
}
_CONVERTED = {
    ".md": ("doc", "text/markdown"),
    ".markdown": ("doc", "text/markdown"),
    ".txt": ("doc", "text/plain"),
    ".html": ("doc", "text/html"),
    ".htm": ("doc", "text/html"),
    ".csv": ("sheet", "text/csv"),
    ".xlsx": (
        "sheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".pptx": (
        "slides",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
}
_APP_KEY = "adk_voice_agent"
_NEEDS_GRANT = f"Google Drive needs permission for this. {GRANT_ACCESS_HINT}"


class WorkspaceWriteError(RuntimeError):
    """A Google Drive call failed with a user-facing message (`needs_grant` when scopes are missing)."""

    def __init__(
        self, message: str, *, status_code: int = 0, needs_grant: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.needs_grant = needs_grant


def is_scope_error(response: httpx.Response) -> bool:
    """True when a 403 indicates missing OAuth scopes rather than file-level permissions."""
    if response.status_code != 403:
        return False
    text = response.text
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    reasons: set[str] = set()
    if isinstance(error, dict):
        for item in [
            *(error.get("details") or []),
            *(error.get("errors") or []),
        ]:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return bool(
        reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}
    ) or ("insufficient authentication scopes" in text.lower())


def save_to_drive_preference(settings: Mapping[str, Any] | None = None) -> str:
    chosen = str((settings or user_settings()).get(SAVE_TO_DRIVE) or "").lower()
    return chosen if chosen in SAVE_CHOICES else SAVE_ON


@dataclass(frozen=True)
class DriveSaving:
    available: bool
    reason: str = ""


def drive_saving(settings: Mapping[str, Any] | None = None) -> DriveSaving:
    from app.auth.providers import WORKSPACE_PROVIDER, has_provider_credentials

    if save_to_drive_preference(settings) == SAVE_OFF:
        return DriveSaving(
            available=False,
            reason="Saving to Drive is off in Settings.",
        )
    if not has_provider_credentials(WORKSPACE_PROVIDER):
        return DriveSaving(
            available=False,
            reason="Connect Google in Settings → Apps to save files to Drive.",
        )
    return DriveSaving(available=True)


def drive_kind(path: str) -> tuple[str, str]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _CONVERTED:
        return _CONVERTED[suffix]
    return "file", mimetypes.guess_type(path)[0] or "application/octet-stream"


def project_title(project: str) -> str:
    """Display title for a project folder (`projects/portland-trip` -> 'Portland trip'; scratch drops date)."""
    name = PurePosixPath(project).name
    if project_kind(project) is ProjectKind.SCRATCH:
        name = name[len("YYYY-MM-DD-") :] or name
    words = name.replace("-", " ").replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else project


async def save_to_drive(
    handle: ThreadHandle, path: str, *, title: str = ""
) -> dict[str, str]:
    """Upload or update `path` from the thread's workdir in the owner's Drive project folder."""
    from app.app_utils.http_client import get_http_client
    from app.auth import bearer_headers, ensure_fresh_access_token
    from app.auth.providers import WORKSPACE_PROVIDER
    from app.projects import project_labels
    from app.workers.factory import resolve_environment

    saving = drive_saving()
    if not saving.available:
        raise ValueError(saving.reason)
    if not handle.workdir:
        raise ValueError(f"{handle.thread_id} has no working directory yet.")
    env = (await resolve_environment()).env
    content = await policy.read_clone_file(
        env,
        handle.workdir,
        path,
        max_bytes=MAX_UPLOAD_BYTES,
        allow_hidden=False,
    )
    token = await ensure_fresh_access_token(WORKSPACE_PROVIDER)
    if not token:
        raise ValueError(
            "Connect Google in Settings → Apps to save files to Drive."
        )
    kind, content_type = drive_kind(path)
    file = PurePosixPath(path)
    name = title.strip() or (file.name if kind == "file" else file.stem)
    labels = await project_labels()
    # Tagged by the project's Drive key, so a kept one-off keeps its folder and files.
    key = labels.drive_key(handle.project)
    async with get_http_client(
        timeout=30.0, headers=bearer_headers(token)
    ) as client:
        root = await _folder(client, ROOT_FOLDER, _tag("root"))
        folder = await _folder(
            client,
            labels.names.get(handle.project) or project_title(handle.project),
            _tag("project", key),
            parent=root,
        )
        source = _tag("file", key, str(file))
        existing = await _find(client, source)
        saved = await upload_drive_file(
            client,
            name,
            content,
            content_type=content_type,
            convert_to=GOOGLE_MIME.get(kind),
            file_id=existing,
            parent=folder,
            app_property=source,
        )
    return {"title": saved["title"], "url": saved["url"], "kind": kind}


async def rename_drive_folder(project_key: str, name: str) -> bool:
    """Best-effort rename of the Drive folder tagged `project_key`."""
    from app.app_utils.http_client import get_http_client
    from app.auth import bearer_headers, ensure_fresh_access_token
    from app.auth.providers import WORKSPACE_PROVIDER

    try:
        token = await ensure_fresh_access_token(WORKSPACE_PROVIDER)
        if not token:
            return False
        async with get_http_client(
            timeout=30.0, headers=bearer_headers(token)
        ) as client:
            folder = await _find(client, _tag("project", project_key))
            if not folder:
                return False
            response = await client.patch(
                f"{DRIVE_FILES_URL}/{folder}",
                params={"fields": "id"},
                json={"name": name},
            )
            return response.status_code == 200
    except Exception:
        logger.warning("Could not rename the Drive folder", exc_info=True)
        return False


async def upload_drive_file(
    client: httpx.AsyncClient,
    name: str,
    content: bytes,
    *,
    content_type: str,
    convert_to: str | None = None,
    file_id: str | None = None,
    parent: str = "",
    app_property: str = "",
) -> dict[str, str]:
    metadata: dict[str, Any] = {"name": name}
    if file_id is None:
        if convert_to:
            metadata["mimeType"] = convert_to
        if parent:
            metadata["parents"] = [parent]
        if app_property:
            metadata["appProperties"] = {_APP_KEY: app_property}
    if content_type.startswith("text/"):
        content_type += "; charset=UTF-8"
    boundary = "===adk_voice_agent_drive_boundary==="
    body = (
        (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    url = f"{DRIVE_UPLOAD_URL}/{file_id}" if file_id else DRIVE_UPLOAD_URL
    response = await client.request(
        "PATCH" if file_id else "POST",
        url,
        params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
        content=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    data = _json(response, "upload")
    return {
        "id": str(data.get("id") or file_id or ""),
        "title": str(data.get("name") or name),
        "url": str(data.get("webViewLink") or ""),
    }


def _tag(kind: str, *parts: str) -> str:
    # Drive caps an appProperty at 124 bytes.
    if not parts:
        return kind
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]
    return f"{kind}:{digest}"


async def _find(client: httpx.AsyncClient, tag: str) -> str | None:
    response = await client.get(
        DRIVE_FILES_URL,
        params={
            "q": f"appProperties has {{ key='{_APP_KEY}' and value='{tag}' }} "
            "and trashed = false",
            "fields": "files(id)",
            "pageSize": "1",
        },
    )
    files = _json(response, "search").get("files") or []
    return str(files[0]["id"]) if files and files[0].get("id") else None


async def _folder(
    client: httpx.AsyncClient, name: str, tag: str, *, parent: str = ""
) -> str:
    found = await _find(client, tag)
    if found:
        return found
    metadata: dict[str, Any] = {
        "name": name,
        "mimeType": FOLDER_MIME,
        "appProperties": {_APP_KEY: tag},
    }
    if parent:
        metadata["parents"] = [parent]
    response = await client.post(
        DRIVE_FILES_URL, params={"fields": "id"}, json=metadata
    )
    return str(_json(response, "folder").get("id") or "")


def _json(response: httpx.Response, what: str) -> dict[str, Any]:
    if response.status_code not in {200, 201}:
        if is_scope_error(response):
            raise WorkspaceWriteError(
                _NEEDS_GRANT, status_code=403, needs_grant=True
            )
        raise WorkspaceWriteError(
            f"Google Drive {what} failed ({response.status_code}): "
            f"{response.text.strip()[:200]}",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}
