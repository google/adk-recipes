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

"""Extracts `{kind, title, url}` links from connected-app MCP tool results for `ThreadRegistry.record_output`."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

Output = dict[str, str]
Extractor = Callable[[dict[str, Any], dict[str, Any]], Output | None]

_PULL_URL = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+")


def result_data(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                data = json.loads(item.get("text") or "")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _output(kind: str, title: Any, url: Any) -> Output | None:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return {"kind": kind, "title": str(title or "").strip(), "url": url}


def _pull_request(data: dict[str, Any], args: dict[str, Any]) -> Output | None:
    url = next(
        (
            u
            for u in (data.get("html_url"), data.get("url"))
            if isinstance(u, str) and _PULL_URL.match(u)
        ),
        None,
    )
    return _output("pr", data.get("title") or args.get("title"), url)


def _doc(data: dict[str, Any], args: dict[str, Any]) -> Output | None:
    doc_id = data.get("document_id") or args.get("document_id")
    if not doc_id:
        return None
    return _output(
        "doc",
        data.get("title"),
        f"https://docs.google.com/document/d/{quote(str(doc_id))}/edit",
    )


def _sheet(data: dict[str, Any], args: dict[str, Any]) -> Output | None:
    sheet_id = data.get("spreadsheet_id") or args.get("spreadsheet_id")
    if not sheet_id:
        return None
    return _output(
        "sheet",
        data.get("title"),
        f"https://docs.google.com/spreadsheets/d/{quote(str(sheet_id))}/edit",
    )


def _event(data: dict[str, Any], args: dict[str, Any]) -> Output | None:
    return _output(
        "event",
        data.get("summary") or args.get("summary"),
        data.get("html_link"),
    )


def _draft(data: dict[str, Any], args: dict[str, Any]) -> Output | None:
    message_id = data.get("message_id")
    if not message_id:
        return None
    title = args.get("subject") or (
        f"Draft to {args['to']}" if args.get("to") else ""
    )
    return _output(
        "draft",
        title,
        f"https://mail.google.com/mail/#drafts?compose={quote(str(message_id))}",
    )


OUTPUT_TOOLS: dict[str, Extractor] = {
    "github.create_pull_request": _pull_request,
    "google_docs.append_text": _doc,
    "google_sheets.update_values": _sheet,
    "google_calendar.create_event": _event,
    "gmail.create_draft": _draft,
}


def gateway_output(
    server: str, tool: str, arguments: dict[str, Any], result: dict[str, Any]
) -> Output | None:
    extract = OUTPUT_TOOLS.get(f"{server}.{tool}")
    if extract is None or result.get("isError"):
        return None
    return extract(result_data(result), arguments)
