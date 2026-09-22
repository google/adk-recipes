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

"""Legacy .xls → Markdown tables using xlrd."""

from __future__ import annotations

import logging
from datetime import datetime

import xlrd

logger = logging.getLogger(__name__)


def _cell_to_str(sheet, row_idx: int, col_idx: int, datemode: int) -> str:
    """Convert an xlrd cell to a string, handling dates properly."""
    cell = sheet.cell(row_idx, col_idx)
    if cell.ctype == xlrd.XL_CELL_DATE:
        dt_tuple = xlrd.xldate_as_tuple(cell.value, datemode)
        dt = datetime(*dt_tuple)
        return (
            dt.strftime("%Y-%m-%d")
            if dt_tuple[3:] == (0, 0, 0)
            else dt.strftime("%Y-%m-%d %H:%M:%S")
        )
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        # Show integers without decimals
        if cell.value == int(cell.value):
            return str(int(cell.value))
        return str(cell.value)
    return str(cell.value)


def _is_empty_row(sheet, row_idx: int) -> bool:
    return all(
        sheet.cell(row_idx, col).ctype == xlrd.XL_CELL_EMPTY
        or str(sheet.cell(row_idx, col).value).strip() == ""
        for col in range(sheet.ncols)
    )


def parse(file_path: str, genai_client=None, max_workers: int = 8) -> str:
    """Extract markdown tables from a .xls file."""
    wb = xlrd.open_workbook(file_path)
    parts = []

    for sheet in wb.sheets():
        if sheet.nrows == 0:
            continue

        non_empty_rows = [
            i for i in range(sheet.nrows) if not _is_empty_row(sheet, i)
        ]
        if not non_empty_rows:
            continue

        parts.append(f"## {sheet.name}")

        table_rows = []
        for row_idx in non_empty_rows:
            cells = [
                _cell_to_str(sheet, row_idx, col, wb.datemode).strip()
                for col in range(sheet.ncols)
            ]
            table_rows.append("| " + " | ".join(cells) + " |")

        if table_rows:
            header_sep = "| " + " | ".join(["---"] * sheet.ncols) + " |"
            table_rows.insert(1, header_sep)
            parts.append("\n".join(table_rows))

    return "\n\n".join(parts)
