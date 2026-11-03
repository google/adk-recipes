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

"""Google Sheets tools (REST: sheets.googleapis.com/v4) for the `google_sheets` server in `.mcp.json`: `python -m workspace_mcp.sheets`."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from workspace_mcp._google import GoogleApi
from workspace_mcp._server import Server

server = Server("google_sheets")

BASE = "https://sheets.googleapis.com/v4/spreadsheets"
MAX_ROWS = 200

_ID = {
    "type": "string",
    "description": "The spreadsheet ID (from its URL: /spreadsheets/d/<id>/).",
}
_RANGE = {
    "type": "string",
    "description": "A1 range, e.g. 'Sheet1!A1:C10' or 'Sheet1'.",
}


def _url(spreadsheet_id: str, *parts: str) -> str:
    path = "/".join(quote(p, safe="") for p in (spreadsheet_id, *parts))
    return f"{BASE}/{path}"


@server.tool(
    "get_spreadsheet",
    "Get a spreadsheet's title and its sheets (tabs) with their sizes.",
    {
        "type": "object",
        "properties": {"spreadsheet_id": _ID},
        "required": ["spreadsheet_id"],
    },
    read_only=True,
)
async def get_spreadsheet(
    api: GoogleApi, args: dict[str, Any]
) -> dict[str, Any]:
    data = await api.get(
        _url(args["spreadsheet_id"]),
        params={"fields": "spreadsheetId,properties.title,sheets.properties"},
    )
    sheets = []
    for sheet in data.get("sheets") or []:
        props = sheet.get("properties") or {}
        grid = props.get("gridProperties") or {}
        sheets.append(
            {
                "sheet_id": props.get("sheetId"),
                "title": props.get("title", ""),
                "rows": grid.get("rowCount"),
                "columns": grid.get("columnCount"),
            }
        )
    return {
        "spreadsheet_id": data.get("spreadsheetId", args["spreadsheet_id"]),
        "title": (data.get("properties") or {}).get("title", ""),
        "sheets": sheets,
    }


@server.tool(
    "read_values",
    f"Read cell values from a range (at most {MAX_ROWS} rows are returned).",
    {
        "type": "object",
        "properties": {"spreadsheet_id": _ID, "range": _RANGE},
        "required": ["spreadsheet_id", "range"],
    },
    read_only=True,
)
async def read_values(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    data = await api.get(_url(args["spreadsheet_id"], "values", args["range"]))
    values = data.get("values") or []
    result: dict[str, Any] = {
        "range": data.get("range", args["range"]),
        "values": values[:MAX_ROWS],
        "total_rows": len(values),
    }
    if len(values) > MAX_ROWS:
        result["truncated"] = True
    return result


@server.tool(
    "update_values",
    "Write values into a range, parsed as if typed (formulas work).",
    {
        "type": "object",
        "properties": {
            "spreadsheet_id": _ID,
            "range": _RANGE,
            "values": {
                "type": "array",
                "description": "Rows of cell values, e.g. [['Name', 'Qty'], "
                "['Pens', 3]].",
                "items": {"type": "array"},
            },
        },
        "required": ["spreadsheet_id", "range", "values"],
    },
)
async def update_values(api: GoogleApi, args: dict[str, Any]) -> dict[str, Any]:
    values = args["values"]
    if not values or not all(isinstance(row, list) for row in values):
        raise ValueError(
            "`values` should be a non-empty array of arrays (rows)"
        )
    data = await api.put(
        _url(args["spreadsheet_id"], "values", args["range"]),
        params={"valueInputOption": "USER_ENTERED"},
        json={"range": args["range"], "values": values},
    )
    return {
        "updated_range": data.get("updatedRange", ""),
        "updated_rows": data.get("updatedRows", 0),
        "updated_columns": data.get("updatedColumns", 0),
        "updated_cells": data.get("updatedCells", 0),
    }


if __name__ == "__main__":
    server.run()
