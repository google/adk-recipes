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
"""The optimization problem: cut the bytes an analytics query scans.

Everything specific to the problem lives here, with no model involved:
the bundled data, the baseline queries a data team actually runs, the rules
that decide whether a rewrite is equivalent, and how runs are compared.

The data is a real public dataset that is also in BigQuery: London Santander
Cycles journeys from Transport for London, as published in
`bigquery-public-data.london_bicycles.cycle_hire`. The bundled sample
(`app/data/cycle_hire.parquet`) holds 1 in 40 journeys started between
2016-03-01 and 2016-05-31 (60,672 rows), with the table's core columns.
Powered by TfL Open Data. See README.md for provenance and licence.
"""

import datetime
import decimal
import math
import re
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).parent / "data" / "cycle_hire.parquet"
DEFAULT_BIGQUERY_TABLE = "bigquery-public-data.london_bicycles.cycle_hire"

# Rewrites longer than this are rejected without running.
MAX_SQL_CHARS = 6_000

SCHEMA: list[dict[str, str]] = [
    {"name": "rental_id", "type": "INT64", "note": "unique per journey"},
    {"name": "duration", "type": "INT64", "note": "seconds"},
    {"name": "bike_id", "type": "INT64", "note": ""},
    {"name": "end_date", "type": "TIMESTAMP", "note": ""},
    {"name": "end_station_id", "type": "INT64", "note": ""},
    {"name": "end_station_name", "type": "STRING", "note": ""},
    {"name": "start_date", "type": "TIMESTAMP", "note": ""},
    {"name": "start_station_id", "type": "INT64", "note": ""},
    {"name": "start_station_name", "type": "STRING", "note": ""},
]

PROBLEM_STATEMENT = """\
Rewrite an analytics SQL query so it returns exactly the same result while
scanning as few bytes as possible. The rewrite must be ONE read-only SELECT
statement (WITH ... SELECT is fine) in the given SQL dialect, against the same
table. Same columns in the same order, same rows (and the same row order when
the baseline has a top-level ORDER BY). Bytes scanned is the score (lower is
better); runtime breaks ties.
"""

# Each baseline is a query a data team would plausibly have in a dashboard or
# a scheduled job, with a common and costly anti-pattern. `{table}` is
# replaced with the engine's table reference.
QUERIES: list[dict[str, Any]] = [
    {
        "id": "commute_flows",
        "title": "Top weekday morning commute routes",
        "question": (
            "Which station-to-station routes are busiest on weekday mornings"
            " (07:00-09:59), and how long do they take on average?"
        ),
        "anti_pattern": "needless SELECT DISTINCT * dedup in a CTE",
        "ordered": True,
        "sql": {
            "duckdb": """\
WITH trips AS (
  SELECT DISTINCT * FROM {table}
)
SELECT start_station_name, end_station_name,
       COUNT(*) AS trips,
       ROUND(AVG(duration) / 60.0, 1) AS avg_minutes
FROM trips
WHERE isodow(start_date) BETWEEN 1 AND 5
  AND hour(start_date) BETWEEN 7 AND 9
  AND start_station_id <> end_station_id
GROUP BY start_station_name, end_station_name
ORDER BY trips DESC, start_station_name, end_station_name
LIMIT 15""",
            "bigquery": """\
WITH trips AS (
  SELECT DISTINCT * FROM `{table}`
  WHERE start_date >= '2016-03-01' AND start_date < '2016-06-01'
)
SELECT start_station_name, end_station_name,
       COUNT(*) AS trips,
       ROUND(AVG(duration) / 60.0, 1) AS avg_minutes
FROM trips
WHERE EXTRACT(DAYOFWEEK FROM start_date) BETWEEN 2 AND 6
  AND EXTRACT(HOUR FROM start_date) BETWEEN 7 AND 9
  AND start_station_id <> end_station_id
GROUP BY start_station_name, end_station_name
ORDER BY trips DESC, start_station_name, end_station_name
LIMIT 15""",
        },
    },
    {
        "id": "station_demand",
        "title": "Busiest start stations in April 2016",
        "question": (
            "Which 10 stations had the most hires in April 2016, and what was"
            " the average ride length?"
        ),
        "anti_pattern": (
            "date filter wrapped in a formatting function, over SELECT *"
        ),
        "ordered": True,
        "sql": {
            "duckdb": """\
SELECT start_station_name,
       COUNT(*) AS hires,
       ROUND(AVG(duration) / 60.0, 1) AS avg_minutes
FROM (SELECT * FROM {table}) AS t
WHERE strftime(start_date, '%Y-%m') = '2016-04'
GROUP BY start_station_name
ORDER BY hires DESC, start_station_name
LIMIT 10""",
            "bigquery": """\
SELECT start_station_name,
       COUNT(*) AS hires,
       ROUND(AVG(duration) / 60.0, 1) AS avg_minutes
FROM (SELECT * FROM `{table}`) AS t
WHERE FORMAT_TIMESTAMP('%Y-%m', start_date) = '2016-04'
GROUP BY start_station_name
ORDER BY hires DESC, start_station_name
LIMIT 10""",
        },
    },
    {
        "id": "monthly_trend",
        "title": "Monthly hires per station, spring 2016",
        "question": (
            "How many hires did each start station have in March, April and"
            " May 2016? Show the 20 busiest stations in May."
        ),
        "anti_pattern": (
            "one CTE per month plus a DISTINCT driver: four scans of the table"
        ),
        "ordered": True,
        "sql": {
            "duckdb": """\
WITH march AS (
  SELECT start_station_id, COUNT(*) AS hires FROM {table}
  WHERE start_date >= TIMESTAMP '2016-03-01'
    AND start_date < TIMESTAMP '2016-04-01'
  GROUP BY start_station_id
),
april AS (
  SELECT start_station_id, COUNT(*) AS hires FROM {table}
  WHERE start_date >= TIMESTAMP '2016-04-01'
    AND start_date < TIMESTAMP '2016-05-01'
  GROUP BY start_station_id
),
may AS (
  SELECT start_station_id, COUNT(*) AS hires FROM {table}
  WHERE start_date >= TIMESTAMP '2016-05-01'
    AND start_date < TIMESTAMP '2016-06-01'
  GROUP BY start_station_id
)
SELECT s.start_station_id,
       COALESCE(march.hires, 0) AS march,
       COALESCE(april.hires, 0) AS april,
       COALESCE(may.hires, 0) AS may
FROM (SELECT DISTINCT start_station_id FROM {table}) AS s
LEFT JOIN march ON march.start_station_id = s.start_station_id
LEFT JOIN april ON april.start_station_id = s.start_station_id
LEFT JOIN may ON may.start_station_id = s.start_station_id
ORDER BY may DESC, s.start_station_id
LIMIT 20""",
            "bigquery": """\
WITH march AS (
  SELECT start_station_id, COUNT(*) AS hires FROM `{table}`
  WHERE start_date >= TIMESTAMP '2016-03-01'
    AND start_date < TIMESTAMP '2016-04-01'
  GROUP BY start_station_id
),
april AS (
  SELECT start_station_id, COUNT(*) AS hires FROM `{table}`
  WHERE start_date >= TIMESTAMP '2016-04-01'
    AND start_date < TIMESTAMP '2016-05-01'
  GROUP BY start_station_id
),
may AS (
  SELECT start_station_id, COUNT(*) AS hires FROM `{table}`
  WHERE start_date >= TIMESTAMP '2016-05-01'
    AND start_date < TIMESTAMP '2016-06-01'
  GROUP BY start_station_id
)
SELECT s.start_station_id,
       COALESCE(march.hires, 0) AS march,
       COALESCE(april.hires, 0) AS april,
       COALESCE(may.hires, 0) AS may
FROM (SELECT DISTINCT start_station_id FROM `{table}`) AS s
LEFT JOIN march ON march.start_station_id = s.start_station_id
LEFT JOIN april ON april.start_station_id = s.start_station_id
LEFT JOIN may ON may.start_station_id = s.start_station_id
ORDER BY may DESC, s.start_station_id
LIMIT 20""",
        },
    },
]

_FENCED_SQL = re.compile(r"```(?:sql)?\s*(.+?)```", re.IGNORECASE | re.DOTALL)
_TOP_LEVEL_ORDER_BY = re.compile(r"\border\s+by\b[^()]*$", re.IGNORECASE)


def query_by_id(query_id: str) -> dict[str, Any]:
    """Returns the bundled query with this id."""
    return next(q for q in QUERIES if q["id"] == query_id)


def select_query(message: str, engine: str, table: str) -> dict[str, Any]:
    """Picks the baseline for a run from the user's message.

    A fenced ```sql block is used as a custom baseline. Otherwise the first
    bundled query whose id or title words appear in the message is used, and
    the first bundled query is the default.

    Returns:
        `id`, `title`, `question`, `anti_pattern`, `ordered` and `sql` (the
        baseline text for `engine`, with `table` filled in).
    """
    fenced = _FENCED_SQL.search(message or "")
    if fenced:
        sql = fenced.group(1).strip().rstrip(";").strip()
        return {
            "id": "custom",
            "title": "Your query",
            "question": "The query you pasted.",
            "anti_pattern": "unknown",
            "ordered": bool(_TOP_LEVEL_ORDER_BY.search(sql)),
            "sql": sql,
        }
    text = (message or "").lower()
    chosen = QUERIES[0]
    for query in QUERIES:
        words = {query["id"], *query["id"].split("_")}
        words |= {w for w in query["title"].lower().split() if len(w) > 4}
        if any(w in text for w in words):
            chosen = query
            break
    return {
        **{k: v for k, v in chosen.items() if k != "sql"},
        "sql": chosen["sql"][engine].format(table=table),
    }


def _normalize(value: Any) -> Any:
    """Makes values comparable across engines and float rounding noise."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, decimal.Decimal)):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if number == int(number) and abs(number) < 2**53:
            return int(number)
        return float(f"{number:.9g}")
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return str(value)


def normalize_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Normalizes every value of every row."""
    return [[_normalize(v) for v in row] for row in rows]


def compare_results(
    expected: list[list[Any]], actual: list[list[Any]], ordered: bool
) -> str | None:
    """Checks that a rewrite returned the baseline's result.

    Rows are compared as an ordered list when the baseline has a top-level
    ORDER BY, otherwise as a multiset.

    Returns:
        None if the results match, otherwise a short, model-readable reason.
    """
    want, got = normalize_rows(expected), normalize_rows(actual)
    if want and got and len(want[0]) != len(got[0]):
        return f"returns {len(got[0])} columns, expected {len(want[0])}"
    if len(want) != len(got):
        return f"returns {len(got)} rows, expected {len(want)}"
    if not ordered:
        want = sorted(want, key=repr)
        got = sorted(got, key=repr)
    for index, (w, g) in enumerate(zip(want, got, strict=True)):
        if w != g:
            where = f"row {index + 1}" if ordered else "a row"
            return f"{where} differs: got {g}, expected {w}"
    return None
