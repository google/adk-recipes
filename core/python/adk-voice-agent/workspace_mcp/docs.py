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

"""Google Docs tools (REST: docs.googleapis.com/v1) for the `google_docs` server in `.mcp.json`: `python -m workspace_mcp.docs`."""

from __future__ import annotations

import re
from typing import Any

from workspace_mcp._google import GoogleApi, truncate
from workspace_mcp._server import Server

BASE = "https://docs.googleapis.com/v1/documents"
TEXT_LIMIT = 20000

_ID = re.compile(r"[A-Za-z0-9_-]+")
_URL_ID = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
_HEADING_LEVELS = {"TITLE": 1, **{f"HEADING_{n}": n for n in range(1, 7)}}

server = Server("google_docs")

_DOCUMENT_ID = {
    "type": "string",
    "description": "The document ID (or its docs.google.com URL).",
}


def _document_id(args: dict[str, Any]) -> str:
    raw = str(args["document_id"]).strip()
    match = _URL_ID.search(raw)
    doc_id = match.group(1) if match else raw
    if not _ID.fullmatch(doc_id):
        raise ValueError(f"`document_id` is not a Docs ID: {raw[:80]!r}")
    return doc_id


def _paragraph_text(paragraph: dict[str, Any]) -> str:
    text = "".join(
        (element.get("textRun") or {}).get("content", "")
        for element in paragraph.get("elements") or ()
    )
    return text.rstrip("\n")


def _content_lines(content: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for element in content or ():
        if "paragraph" in element:
            paragraph = element["paragraph"]
            text = _paragraph_text(paragraph)
            style = (paragraph.get("paragraphStyle") or {}).get(
                "namedStyleType", ""
            )
            level = _HEADING_LEVELS.get(style)
            if level and text.strip():
                text = f"{'#' * level} {text.strip()}"
            lines.append(text)
        elif "table" in element:
            for row in element["table"].get("tableRows") or ():
                cells = (
                    " ".join(
                        line.strip()
                        for line in _content_lines(cell.get("content") or [])
                        if line.strip()
                    )
                    for cell in row.get("tableCells") or ()
                )
                lines.append(" | ".join(cells))
        elif "tableOfContents" in element:
            toc = element["tableOfContents"].get("content") or []
            lines.extend(_content_lines(toc))
    return lines


@server.tool(
    "get_document",
    "Read a Google Doc as plain text: headings as Markdown `#` lines, "
    "table rows as cells separated by ' | '.",
    {
        "type": "object",
        "properties": {"document_id": _DOCUMENT_ID},
        "required": ["document_id"],
    },
    read_only=True,
)
async def get_document(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    doc_id = _document_id(args)
    doc = await api.get(f"{BASE}/{doc_id}")
    body = (doc.get("body") or {}).get("content") or []
    text, cut = truncate("\n".join(_content_lines(body)).strip(), TEXT_LIMIT)
    result: dict[str, Any] = {
        "document_id": doc.get("documentId") or doc_id,
        "title": doc.get("title", ""),
        "text": text,
    }
    if cut:
        result["truncated"] = True
    return result


@server.tool(
    "append_text",
    "Append text to the end of a Google Doc's body. Include a leading "
    "newline to start a new paragraph.",
    {
        "type": "object",
        "properties": {
            "document_id": _DOCUMENT_ID,
            "text": {"type": "string", "description": "The text to append."},
        },
        "required": ["document_id", "text"],
    },
)
async def append_text(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    doc_id = _document_id(args)
    text = args["text"]
    requests = [{"insertText": {"text": text, "endOfSegmentLocation": {}}}]
    await api.post(f"{BASE}/{doc_id}:batchUpdate", json={"requests": requests})
    return {"document_id": doc_id, "appended_chars": len(text)}


if __name__ == "__main__":
    server.run()
