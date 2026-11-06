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
"""Runs untrusted candidate SQL against the bundled data in a separate process.

Candidates are SQL text, never code. Before anything runs, the SQL must parse
as exactly one read-only SELECT statement (DuckDB's own parser decides). It
then runs in a fresh, isolated Python interpreter (`python -I -B`) with an
empty environment, a wall-clock timeout and, on POSIX, CPU / memory / file
size limits. Inside that process DuckDB:

- reads the data through a metering filesystem, which counts every byte read
  from the Parquet file (the score);
- may read only that one file: external access is disabled and the
  configuration is locked before the candidate runs, so it cannot read other
  files, write files, attach databases or install extensions;
- runs single-threaded, so bytes read and runtime are reproducible.

NOTE: This is process isolation, not a security sandbox. Run the recipe in a
container (see the Dockerfile) and do not expose it to untrusted users without
a stronger sandbox such as gVisor.
"""

import json
import os
import subprocess
import sys
from typing import Any

import duckdb

_MAX_ERROR_CHARS = 500
_MAX_RESULT_ROWS = 10_000
TABLE = "cycle_hire"

_HARNESS = r"""
import datetime, decimal, json, statistics, sys, time

import duckdb
from fsspec.implementations.local import LocalFileSystem

payload = json.load(sys.stdin)


class Metered(LocalFileSystem):
    protocol = "metered"
    root_marker = "/"
    bytes_read = 0

    @classmethod
    def _strip_protocol(cls, path):
        path = str(path)
        if path.startswith("metered://"):
            path = path[len("metered://"):]
        return LocalFileSystem._strip_protocol(path)

    def _open(self, path, mode="rb", **kwargs):
        handle = super()._open(self._strip_protocol(path), mode, **kwargs)
        read = handle.read

        def metered_read(size=-1):
            data = read(size)
            Metered.bytes_read += len(data)
            return data

        handle.read = metered_read
        return handle


def connect():
    uri = "metered://" + payload["data_path"]
    con = duckdb.connect()
    con.register_filesystem(Metered())
    con.execute("SET threads = 1")
    con.execute("SET memory_limit = '512MB'")
    con.execute("SET enable_external_file_cache = false")
    con.execute(
        f"CREATE VIEW {payload['table']} AS SELECT * FROM read_parquet('{uri}')"
    )
    con.execute(f"SET allowed_paths = ['{uri}']")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def plain(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


timings, scanned, rows, columns = [], None, None, None
for _ in range(payload["repeats"]):
    con = connect()
    Metered.bytes_read = 0
    start = time.perf_counter()
    cursor = con.execute(payload["sql"])
    result = cursor.fetchmany(payload["max_rows"] + 1)
    timings.append(time.perf_counter() - start)
    if scanned is None:
        scanned = Metered.bytes_read
        columns = [d[0] for d in cursor.description]
        rows = [[plain(v) for v in row] for row in result]
    con.close()

sys.stdout.write(json.dumps({
    "bytes_scanned": scanned,
    "runtime_ms": round(statistics.median(timings) * 1000, 2),
    "columns": columns,
    "rows": rows,
}))
"""


def validate_sql(sql: str) -> str | None:
    """Checks that `sql` is exactly one read-only SELECT statement.

    Returns:
        None if it is, otherwise a short, model-readable reason.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        return f"does not parse: {str(error).splitlines()[0]}"
    if len(statements) != 1:
        return f"must be exactly one statement, got {len(statements)}"
    if statements[0].type != duckdb.StatementType.SELECT:
        return f"must be a SELECT, got {statements[0].type.name}"
    return None


def _limit_resources(
    cpu_seconds: int,
) -> None:  # pragma: no cover - runs in child
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def run_sql(
    sql: str,
    data_path: str,
    timeout_seconds: float = 10.0,
    repeats: int = 3,
) -> dict[str, Any]:
    """Runs one SELECT against the data file and meters what it reads.

    Returns:
        `{"ok": True, "bytes_scanned", "runtime_ms", "columns", "rows"}` on
        success, otherwise `{"ok": False, "error": "..."}` with a short,
        model-readable reason. Runtime is the median of `repeats` runs, each
        on a fresh connection.
    """
    problem = validate_sql(sql)
    if problem:
        return {"ok": False, "error": problem}

    preexec = None
    if os.name == "posix":
        cpu = max(1, int(timeout_seconds))

        def preexec() -> None:  # pragma: no cover - runs in child
            _limit_resources(cpu)

    payload = {
        "sql": sql,
        "data_path": os.path.abspath(data_path),
        "table": TABLE,
        "repeats": repeats,
        "max_rows": _MAX_RESULT_ROWS,
    }
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv; isolation is the point
            [sys.executable, "-I", "-B", "-c", _HARNESS],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={},
            timeout=timeout_seconds,
            preexec_fn=preexec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout_seconds:.0f}s"}

    if proc.returncode != 0:
        stderr = proc.stderr.strip().splitlines()
        errors = [line for line in stderr if "Error" in line]
        reason = (errors or stderr or [f"exit code {proc.returncode}"])[-1]
        return {"ok": False, "error": reason[:_MAX_ERROR_CHARS]}
    result = json.loads(proc.stdout)
    if len(result["rows"]) > _MAX_RESULT_ROWS:
        return {
            "ok": False,
            "error": f"returns more than {_MAX_RESULT_ROWS} rows",
        }
    return {"ok": True, **result}
