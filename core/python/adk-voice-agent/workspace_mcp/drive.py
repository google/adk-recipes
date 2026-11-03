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

"""Google Drive tools (REST: www.googleapis.com/drive/v3) for the `google_drive` server in `.mcp.json`: `python -m workspace_mcp.drive`."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from workspace_mcp._google import GoogleApi, truncate
from workspace_mcp._server import Server

server = Server("google_drive")

BASE = "https://www.googleapis.com/drive/v3"
DEFAULT_RESULTS = 20
MAX_RESULTS = 50
TEXT_LIMIT = 20_000
_DOWNLOAD_BYTES = 4 * TEXT_LIMIT

_SEARCH_FIELDS = "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink,owners(displayName))"
_FILE_FIELDS = "id,name,mimeType,size,webViewLink"

_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
_TEXT_TYPES = frozenset(
    (
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/x-yaml",
        "application/yaml",
        "application/x-sh",
        "application/sql",
        "application/csv",
    )
)


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _is_text(mime_type: str) -> bool:
    return (
        mime_type.startswith("text/")
        or mime_type in _TEXT_TYPES
        or mime_type.endswith(("+json", "+xml"))
    )


@server.tool(
    "search_files",
    "Search Drive files by name and content (newest first without a query). "
    "Returns file IDs for read_file.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Words to find in file names or content.",
            },
            "mime_type": {
                "type": "string",
                "description": "Only this MIME type, e.g. "
                "application/vnd.google-apps.document or application/pdf.",
            },
            "max_results": {
                "type": "integer",
                "description": f"How many files (default {DEFAULT_RESULTS}, "
                f"max {MAX_RESULTS}).",
            },
            "page_token": {
                "type": "string",
                "description": "next_page_token from a previous search.",
            },
        },
        "required": [],
    },
    read_only=True,
)
async def search_files(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    mime_type = (args.get("mime_type") or "").strip()
    max_results = args.get("max_results")
    if max_results is None:
        max_results = DEFAULT_RESULTS
    if max_results < 1:
        raise ValueError("`max_results` should be at least 1")
    clauses = []
    if query:
        literal = _quoted(query)
        clauses.append(
            f"(name contains {literal} or fullText contains {literal})"
        )
    clauses.append("trashed = false")
    if mime_type:
        clauses.append(f"mimeType = {_quoted(mime_type)}")
    params: dict[str, Any] = {
        "q": " and ".join(clauses),
        "pageSize": min(max_results, MAX_RESULTS),
        "fields": _SEARCH_FIELDS,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if not query:  # fullText searches can't be ordered; others go newest first.
        params["orderBy"] = "modifiedTime desc"
    if args.get("page_token"):
        params["pageToken"] = args["page_token"]
    reply = await api.get(f"{BASE}/files", params=params)
    result: dict[str, Any] = {
        "files": [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mime_type": f.get("mimeType"),
                "modified_time": f.get("modifiedTime"),
                "web_view_link": f.get("webViewLink"),
            }
            for f in reply.get("files") or []
        ]
    }
    if reply.get("nextPageToken"):
        result["next_page_token"] = reply["nextPageToken"]
    return result


@server.tool(
    "read_file",
    "Read a Drive file's text: Docs and Slides as plain text, Sheets as CSV "
    "(first sheet), and text files as is.",
    {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "The file ID from search_files.",
            },
        },
        "required": ["file_id"],
    },
    read_only=True,
)
async def read_file(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    file_id = args["file_id"].strip()
    if not file_id or "/" in file_id:
        raise ValueError(
            "`file_id` should be a Drive file ID (from search_files), not a URL"
        )
    url = f"{BASE}/files/{quote(file_id, safe='')}"
    meta = await api.get(
        url, params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"}
    )
    mime_type = meta.get("mimeType") or ""
    result: dict[str, Any] = {
        "id": meta.get("id") or file_id,
        "name": meta.get("name"),
        "mime_type": mime_type,
        "web_view_link": meta.get("webViewLink"),
    }
    cut = False
    if mime_type in _EXPORTS:
        text = await api.get_text(
            f"{url}/export", params={"mimeType": _EXPORTS[mime_type]}
        )
    elif _is_text(mime_type):
        text = await api.get_text(
            url,
            params={"alt": "media", "supportsAllDrives": "true"},
            headers={"Range": f"bytes=0-{_DOWNLOAD_BYTES - 1}"},
        )
        cut = int(meta.get("size") or 0) > _DOWNLOAD_BYTES
    else:
        result["note"] = (
            f"{mime_type or 'this file type'} isn't readable as text here; "
            "open web_view_link instead."
        )
        return result
    result["text"], truncated = truncate(text, TEXT_LIMIT)
    if truncated or cut:
        result["truncated"] = True
    return result


if __name__ == "__main__":
    server.run()
