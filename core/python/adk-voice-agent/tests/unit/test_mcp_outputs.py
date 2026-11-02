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

"""Outputs read from connected-app results: links to what a thread made."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.mcp_outputs import gateway_output
from app.api.serializers import output_links
from app.threads.model import EventKind, ThreadHandle, ThreadState


def _text(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@pytest.mark.parametrize(
    ("server", "tool", "args", "result", "expected"),
    [
        (
            "github",
            "create_pull_request",
            {"title": "Fix login"},
            _text({"id": "1", "url": "https://github.com/acme/app/pull/7"}),
            {
                "kind": "pr",
                "title": "Fix login",
                "url": "https://github.com/acme/app/pull/7",
            },
        ),
        (
            "github",
            "create_pull_request",
            {},
            _text(
                {
                    "title": "Fix login",
                    "url": "https://api.github.com/repos/acme/app/pulls/7",
                    "html_url": "https://github.com/acme/app/pull/7",
                }
            ),
            {
                "kind": "pr",
                "title": "Fix login",
                "url": "https://github.com/acme/app/pull/7",
            },
        ),
        (
            "google_docs",
            "append_text",
            {"document_id": "doc1", "text": "Day 2"},
            {"structuredContent": {"document_id": "doc1", "appended_chars": 5}},
            {
                "kind": "doc",
                "title": "",
                "url": "https://docs.google.com/document/d/doc1/edit",
            },
        ),
        (
            "google_sheets",
            "update_values",
            {"spreadsheet_id": "s1", "range": "A1", "values": [[1]]},
            {"structuredContent": {"updated_cells": 1}},
            {
                "kind": "sheet",
                "title": "",
                "url": "https://docs.google.com/spreadsheets/d/s1/edit",
            },
        ),
        (
            "google_calendar",
            "create_event",
            {"summary": "Dentist"},
            {
                "structuredContent": {
                    "id": "e1",
                    "html_link": "https://www.google.com/calendar/event?eid=e1",
                }
            },
            {
                "kind": "event",
                "title": "Dentist",
                "url": "https://www.google.com/calendar/event?eid=e1",
            },
        ),
    ],
)
def test_a_write_names_what_it_made(server, tool, args, result, expected):
    assert gateway_output(server, tool, args, result) == expected


def test_reads_failures_and_results_without_a_link_make_nothing():
    pr = _text({"url": "https://github.com/acme/app/pull/7"})
    assert gateway_output("github", "get_pull_request", {}, pr) is None
    assert (
        gateway_output(
            "github", "create_pull_request", {}, {**pr, "isError": True}
        )
        is None
    )
    assert (
        gateway_output("github", "create_pull_request", {}, _text({"id": "1"}))
        is None
    )
    assert (
        gateway_output(
            "google_calendar",
            "create_event",
            {},
            {"content": [{"type": "text", "text": "oops"}]},
        )
        is None
    )


def test_a_pull_request_replaces_its_branch_and_leads_the_links():
    handle = ThreadHandle(
        thread_id="thread-1",
        goal="Fix login",
        project="code/app",
        harness="claude",
        branch="agent/thread-1",
        state=ThreadState.COMPLETED,
    )
    for output in (
        {"kind": "doc", "title": "Notes", "url": "https://docs.google.com/d/1"},
        {
            "kind": "pr",
            "title": "Fix login",
            "url": "https://github.com/a/b/pull/7",
        },
        # The same Doc again, untitled: the link keeps its title.
        {"kind": "doc", "title": "", "url": "https://docs.google.com/d/1"},
    ):
        handle.record(EventKind.PROGRESS, "made", {"output": output})

    assert [
        (link["kind"], link["title"])
        for link in output_links(handle, handle.outputs, pushed=True)
    ] == [("pr", "Fix login"), ("doc", "Notes")]
