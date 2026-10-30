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

"""The `google_sheets` server's tools against a fake Sheets REST API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from workspace_mcp._google import GoogleApi
from workspace_mcp.sheets import MAX_ROWS, server


def _api(body: Any, status: int = 200) -> tuple[GoogleApi, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body)

    return GoogleApi(
        "fake-token", transport=httpx.MockTransport(reply)
    ), requests


@pytest.mark.asyncio
async def test_get_spreadsheet_lists_sheets_with_sizes() -> None:
    api, requests = _api(
        {
            "spreadsheetId": "s1",
            "properties": {"title": "Budget"},
            "sheets": [
                {
                    "properties": {
                        "sheetId": 0,
                        "title": "Q1",
                        "gridProperties": {"rowCount": 100, "columnCount": 26},
                    }
                }
            ],
        }
    )
    result = await server.call_tool(
        "get_spreadsheet", {"spreadsheet_id": "s1"}, api=api
    )
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "spreadsheet_id": "s1",
        "title": "Budget",
        "sheets": [{"sheet_id": 0, "title": "Q1", "rows": 100, "columns": 26}],
    }
    (request,) = requests
    assert request.method == "GET"
    assert request.url.host == "sheets.googleapis.com"
    assert request.url.path == "/v4/spreadsheets/s1"
    assert request.url.params["fields"] == (
        "spreadsheetId,properties.title,sheets.properties"
    )


@pytest.mark.asyncio
async def test_read_values_encodes_the_range() -> None:
    api, requests = _api(
        {"range": "'My Sheet'!A1:B2", "values": [["a", "b"], ["1", "2"]]}
    )
    result = await server.call_tool(
        "read_values",
        {"spreadsheet_id": "s1", "range": "'My Sheet'!A1:B2"},
        api=api,
    )
    assert result["structuredContent"] == {
        "range": "'My Sheet'!A1:B2",
        "values": [["a", "b"], ["1", "2"]],
        "total_rows": 2,
    }
    (request,) = requests
    assert request.method == "GET"
    assert request.url.raw_path == (
        b"/v4/spreadsheets/s1/values/%27My%20Sheet%27%21A1%3AB2"
    )


@pytest.mark.asyncio
async def test_read_values_caps_rows() -> None:
    rows = [[str(i)] for i in range(MAX_ROWS + 5)]
    api, _ = _api({"range": "Sheet1!A1:A205", "values": rows})
    result = await server.call_tool(
        "read_values", {"spreadsheet_id": "s1", "range": "Sheet1"}, api=api
    )
    content = result["structuredContent"]
    assert len(content["values"]) == MAX_ROWS
    assert content["truncated"] is True
    assert content["total_rows"] == MAX_ROWS + 5


@pytest.mark.asyncio
async def test_read_values_of_an_empty_range() -> None:
    api, _ = _api({"range": "Sheet1!A1:B2"})
    result = await server.call_tool(
        "read_values", {"spreadsheet_id": "s1", "range": "A1:B2"}, api=api
    )
    assert result["structuredContent"] == {
        "range": "Sheet1!A1:B2",
        "values": [],
        "total_rows": 0,
    }


@pytest.mark.asyncio
async def test_update_values_writes_user_entered() -> None:
    api, requests = _api(
        {
            "spreadsheetId": "s1",
            "updatedRange": "Sheet1!A1:B2",
            "updatedRows": 2,
            "updatedColumns": 2,
            "updatedCells": 4,
        }
    )
    values = [["Name", "Qty"], ["Pens", 3]]
    result = await server.call_tool(
        "update_values",
        {"spreadsheet_id": "s1", "range": "Sheet1!A1", "values": values},
        api=api,
    )
    assert result["structuredContent"] == {
        "updated_range": "Sheet1!A1:B2",
        "updated_rows": 2,
        "updated_columns": 2,
        "updated_cells": 4,
    }
    (request,) = requests
    assert request.method == "PUT"
    assert request.url.raw_path.startswith(
        b"/v4/spreadsheets/s1/values/Sheet1%21A1?"
    )
    assert request.url.params["valueInputOption"] == "USER_ENTERED"
    assert json.loads(request.content) == {
        "range": "Sheet1!A1",
        "values": values,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], "array of arrays"),
        (["a", "b"], "array of arrays"),
        ("a,b", "should be a array"),
    ],
)
async def test_update_values_rejects_bad_values(
    values: Any, expected: str
) -> None:
    api, requests = _api({})
    result = await server.call_tool(
        "update_values",
        {"spreadsheet_id": "s1", "range": "A1", "values": values},
        api=api,
    )
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert not requests


@pytest.mark.asyncio
async def test_missing_arguments_are_rejected() -> None:
    api, requests = _api({})
    result = await server.call_tool(
        "read_values", {"spreadsheet_id": "s1"}, api=api
    )
    assert result["isError"] is True
    assert "range" in result["content"][0]["text"]
    assert not requests


@pytest.mark.asyncio
async def test_a_missing_spreadsheet_hints_at_the_id() -> None:
    api, _ = _api(
        {"error": {"code": 404, "message": "Requested entity was not found."}},
        status=404,
    )
    result = await server.call_tool(
        "get_spreadsheet", {"spreadsheet_id": "nope"}, api=api
    )
    assert result["isError"] is True
    assert "not found: check the ID" in result["content"][0]["text"]


def test_annotations() -> None:
    hints = {n: t.annotations for n, t in server.tools.items()}
    assert set(hints) == {"get_spreadsheet", "read_values", "update_values"}
    assert hints["get_spreadsheet"]["readOnlyHint"] is True
    assert hints["read_values"]["readOnlyHint"] is True
    assert hints["update_values"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": False,
    }
