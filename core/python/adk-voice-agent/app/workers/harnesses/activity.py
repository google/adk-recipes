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

"""Plain-language activity lines for connected-app and bridge MCP tool calls."""

from __future__ import annotations

from app.workers.harnesses.base import MCP_SERVER_NAME

APP_NAMES = {
    "google_calendar": "your calendar",
    "gmail": "your email",
    "google_drive": "your Drive",
    "google_docs": "your Docs",
    "google_sheets": "your Sheets",
    "google_slides": "your Slides",
    "github": "GitHub",
}

TOOL_LINES = {
    "github__create_pull_request": "Opening a pull request",
    "gmail__create_draft": "Drafting an email",
    "gmail__send_message": "Sending an email",
    "google_calendar__create_event": "Adding to your calendar",
    "list_threads": "Checking your other threads",
    "read_thread": "Checking another thread",
    "update_user": "Updating you",
    "save_to_drive": "Saving to your Drive",
    "present_card": "Putting a card on screen",
    "recall": "Checking what it knows about you",
    "recall_conversation": "Recalling an earlier conversation",
    "search_web_grounded": "Searching the web",
    "search_maps_grounded": "Searching Maps",
}

_VERBS = {
    **dict.fromkeys(
        ("list", "search", "get", "read", "find", "query", "fetch"), "Checking"
    ),
    **dict.fromkeys(
        ("create", "add", "insert", "append", "update", "write", "edit", "set"),
        "Updating",
    ),
    **dict.fromkeys(("delete", "remove", "trash"), "Removing from"),
    "send": "Sending from",
}


def mcp_activity(name: str) -> str:
    tool = name.removeprefix("mcp__").removeprefix(f"{MCP_SERVER_NAME}__")
    if line := TOOL_LINES.get(tool):
        return line
    server, _, action = tool.partition("__")
    app = APP_NAMES.get(server)
    if app is None:
        return "Using a connected app"
    verb = _VERBS.get(action.split("_", 1)[0], "Using")
    return f"{verb} {app}"
