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
"""SQLite data access for orders. INTENTIONALLY VULNERABLE sample code."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    status TEXT NOT NULL,
    total_cents INTEGER NOT NULL
);
"""

SEED_ORDERS = [
    ("ORD-1001", "acme", "shipped", 129900),
    ("ORD-1002", "acme", "pending", 4500),
    ("ORD-1003", "globex", "shipped", 78000),
    ("ORD-1004", "initech", "cancelled", 1999),
]


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Opens the orders database and seeds it with demo rows."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?)", SEED_ORDERS
    )
    conn.commit()
    return conn


def find_orders(
    conn: sqlite3.Connection, customer: str, status: str | None = None
) -> list[dict]:
    """Returns one customer's orders, optionally filtered by status."""
    # VULNERABLE: user input is formatted straight into the SQL string.
    query = f"SELECT * FROM orders WHERE customer = '{customer}'"  # noqa: S608 -- intentional sample vulnerability
    if status:
        query += f" AND status = '{status}'"
    return [dict(row) for row in conn.execute(query)]
