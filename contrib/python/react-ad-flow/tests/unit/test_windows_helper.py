"""Shared attribution-window loader (ws10 carried items 1+2)."""

import sqlite3
from datetime import datetime

from app.demo_data.windows import load_attribution_windows


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE video_attribution ("
        "video_id INTEGER, ad_campaign_id INTEGER, screen_id INTEGER, "
        "active_from TEXT NOT NULL, active_to TEXT)"
    )
    conn.execute(
        "INSERT INTO video_attribution VALUES (7, 101, 10100, '2026-06-01T00:00:00', NULL)"
    )
    conn.execute(
        "INSERT INTO video_attribution VALUES "
        "(7, 101, 10101, '2026-06-01T00:00:00', '2026-06-05T12:00:00')"
    )
    conn.execute(
        "INSERT INTO video_attribution VALUES (8, 101, 10100, '2026-06-02T00:00:00', NULL)"
    )
    return conn


class _ExplodingCursor:
    """Fails the test if any SQL is executed."""

    def execute(self, *a, **k):
        raise AssertionError("empty video_ids must not touch the DB")


class TestLoadAttributionWindows:
    def test_empty_video_ids_returns_empty_without_sql(self):
        # Carried item 1: '... IN ()' is invalid SQLite; guard must short-circuit.
        assert load_attribution_windows(_ExplodingCursor(), []) == []

    def test_loads_windows_in_join_ready_shape(self):
        cur = _make_db().cursor()
        windows = load_attribution_windows(cur, [7])
        assert len(windows) == 2
        w_open = next(w for w in windows if w["screen_id"] == 10100)
        assert w_open == {
            "video_id": 7,
            "screen_id": 10100,
            "active_from": datetime(2026, 6, 1),
            "active_to": None,
        }
        w_closed = next(w for w in windows if w["screen_id"] == 10101)
        assert w_closed["active_to"] == datetime(2026, 6, 5, 12)

    def test_works_with_sqlite3_row_factory(self):
        # review_tools cursors use sqlite3.Row (app/database/db.py:29);
        # mock_data cursors may be plain tuples — helper must serve both.
        conn = _make_db()
        conn.row_factory = sqlite3.Row
        windows = load_attribution_windows(conn.cursor(), [7, 8])
        assert {w["video_id"] for w in windows} == {7, 8}

    def test_review_tools_alias_is_the_shared_helper(self):
        # Carried item 2: one implementation, two import sites.
        from app.tools import review_tools

        assert (
            review_tools._load_attribution_windows is load_attribution_windows
        )
